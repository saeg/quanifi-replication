#!/usr/bin/env python3
"""Capture the actual supplementary NiFi canvases with a private token file.

Adapted from Quanifi tools/nifi_screenshot.py. Uses Chrome DevTools Protocol
and the Python standard library; no Pillow or browser driver is required.
Self-signed certificates are accepted only for the local demonstration URL.
"""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9335
NIFI = os.environ.get("NIFI_URL", "https://localhost:8450")
COOKIE = "__Secure-Authorization-Bearer"


class WS:
    """Minimum viable websocket client: handshake, send text, receive text."""

    def __init__(self, url):
        _, rest = url.split("://", 1)
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if accept.encode() not in buf:
            raise RuntimeError("websocket handshake rejected")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _send_frame(self, payload):
        data = payload.encode()
        header = bytearray([0x81])           # FIN + text
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126); header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127); header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv_frame(self):
        b0, b1 = self._read(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(length)
        if b0 & 0x0F == 0x08:                # close
            raise RuntimeError("closed by peer")
        return payload.decode("utf-8", "replace")

    def call(self, method, params=None, timeout=90):
        self.next_id += 1
        mid = self.next_id
        self._send_frame(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self._recv_frame())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(method)


def start_chrome(profile, width=2000, height=1300):
    global PORT
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        PORT = listener.getsockname()[1]
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--ignore-certificate-errors",
         "--hide-scrollbars", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={profile}", f"--window-size={width},{height}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=2) as r:
                json.load(r)
                return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("chrome did not expose its debug port")


def page_target():
    """Reuse the about:blank tab Chrome already opened.

    /json/new wants PUT in current Chrome and answers 405 to anything else, so
    taking the existing page target is both simpler and version-proof.
    """
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=10) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["webSocketDebuggerUrl"]



HIDE_PANELS_CSS = """
  navigation-control, operation-control, .navigation-control, .operation-control,
  .birdseye-container, .context-menu { display: none !important; }
"""


def js(ws, expression, timeout=90):
    r = ws.call("Runtime.evaluate",
                {"expression": expression, "returnByValue": True}, timeout=timeout)
    return r.get("result", {}).get("value")


def api(path, token):
    req = urllib.request.Request(NIFI + "/nifi-api" + path,
                                 headers={"Authorization": "Bearer " + token})
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.load(r)


def mint_token(user, password):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = urllib.parse.urlencode({"username": user, "password": password}).encode()
    req = urllib.request.Request(NIFI + "/nifi-api/access/token", data=data)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return r.read().decode().strip()


def capture(token, group, png_path, rects_path, width, height, scale, wait):
    # The Chrome profile is a few dozen megabytes of caches and must not land in
    # the output directory, which is a documentation folder under version control.
    import tempfile, shutil
    profile = tempfile.mkdtemp(prefix="nifi-shot-")
    proc = start_chrome(profile, width, height)
    try:
        ws = WS(page_target())
        ws.call("Security.setIgnoreCertificateErrors", {"ignore": True})
        for domain in ("Network", "Page", "Runtime"):
            ws.call(domain + ".enable")
        ws.call("Network.setCookie", {"name": COOKIE, "value": token,
                                      "domain": "localhost", "path": "/",
                                      "secure": True, "url": NIFI + "/"})
        ws.call("Emulation.setDeviceMetricsOverride",
                {"width": width, "height": height,
                 "deviceScaleFactor": scale, "mobile": False})
        ws.call("Page.navigate", {"url": "%s/nifi/#/process-groups/%s" % (NIFI, group)})
        time.sleep(wait)
        for _ in range(40):
            if js(ws, "document.querySelectorAll('g.processor').length > 0"):
                break
            time.sleep(0.5)

        js(ws, """
          (() => {
            const b = [...document.querySelectorAll('button')].find(x => /fit/i.test(
              (x.getAttribute('title') || '') + (x.getAttribute('aria-label') || '')));
            if (b) b.click();
          })()
        """)
        time.sleep(3)
        js(ws, "(() => { const s = document.createElement('style');"
               "s.textContent = %s; document.head.appendChild(s); })()"
               % json.dumps(HIDE_PANELS_CSS))
        time.sleep(1)

        rects = js(ws, """
          (() => {
            const out = {};
            document.querySelectorAll('g.processor, g.label, g.funnel').forEach(g => {
              const id = (g.getAttribute('id') || '').replace(/^id-/, '');
              const r = g.getBoundingClientRect();
              if (id && r.width > 0) out[id] = [r.left, r.top, r.width, r.height];
            });
            return JSON.stringify(out);
          })()
        """)
        if not rects or not json.loads(rects):
            raise RuntimeError("No NiFi processors were rendered; refusing to save a misleading screenshot")
        open(rects_path, "w").write(rects)
        capture_height = min(height, max(r[1] + r[3] for r in json.loads(rects).values()) + 55)
        shot = ws.call("Page.captureScreenshot", {"format": "png", "clip": {
            "x": 0, "y": 0, "width": width, "height": capture_height, "scale": 1}})
        open(png_path, "wb").write(base64.b64decode(shot["data"]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(profile, ignore_errors=True)



def main():
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--width', type=int, default=2200)
    parser.add_argument('--height', type=int, default=1000)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    token = Path(args.token_file).read_text().strip()
    deployment = json.loads((root / 'evidence/deployment.json').read_text())
    for group in deployment['groups']:
        capture(token, group['group_id'], str(root / 'assets' / (group['id'] + '.png')),
                str(root / 'evidence' / (group['id'] + '-canvas-geometry.json')),
                args.width, args.height, 1.5, 10)
        print('Captured', group['id'], flush=True)


if __name__ == '__main__':
    main()
