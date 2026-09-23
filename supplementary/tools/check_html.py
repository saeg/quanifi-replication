#!/usr/bin/env python3
"""Check all gallery pages offline in Chrome at desktop and mobile widths."""
import base64
import json
import shutil
import tempfile
import time
from pathlib import Path

import nifi_screenshot as browser

ROOT = Path(__file__).resolve().parents[1]


def main():
    profile = tempfile.mkdtemp(prefix='quanifi-html-check-')
    proc = browser.start_chrome(profile, 1440, 1100)
    try:
        ws = browser.WS(browser.page_target())
        ws.call('Page.enable')
        ws.call('Network.enable')
        ws.call('Network.setBlockedURLs', {'urls': ['http://*', 'https://*']})
        records = []
        for width in [1440, 390]:
            ws.call('Emulation.setDeviceMetricsOverride', {
                'width': width, 'height': 1100, 'deviceScaleFactor': 1, 'mobile': False,
            })
            for page in ['index', 'deutsch-jozsa', 'bernstein-vazirani', 'portfolio-qaoa', 'max-independent-set']:
                ws.call('Page.navigate', {'url': (ROOT / (page + '.html')).as_uri()})
                time.sleep(0.8)
                browser.js(ws, "document.querySelectorAll('img').forEach(x=>x.loading='eager')")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if browser.js(ws, "[...document.images].every(x=>x.complete&&x.naturalWidth>0)"):
                        break
                    time.sleep(0.2)
                state = browser.js(ws, "({title:document.title,h1:document.querySelectorAll('h1').length,overflow:document.documentElement.scrollWidth>innerWidth,images:[...document.images].every(x=>x.complete&&x.naturalWidth>0)})")
                assert state['h1'] == 1 and not state['overflow'] and state['images'], (page, width, state)
                browser.js(ws, "document.querySelectorAll('details').forEach(x=>x.open=true)")
                assert not browser.js(ws, 'document.documentElement.scrollWidth>innerWidth'), (page, 'expanded overflow')
                browser.js(ws, "document.querySelectorAll('details').forEach(x=>x.open=false)")
                if page == 'portfolio-qaoa':
                    before = browser.js(ws, "document.querySelector('output').textContent")
                    browser.js(ws, "document.getElementById('asset-0').click()")
                    after = browser.js(ws, "document.querySelector('output').textContent")
                    assert '011' in before and '111' in after and '2.6650' in after
                    browser.js(ws, "document.getElementById('asset-0').click()")
                if page == 'max-independent-set':
                    before = browser.js(ws, "document.querySelector('output').textContent")
                    browser.js(ws, "document.getElementById('vertex-2').click()")
                    after = browser.js(ws, "document.querySelector('output').textContent")
                    assert 'independent' in before and '2 conflicting edges' in after
                    browser.js(ws, "document.getElementById('vertex-2').click()")
                if (width == 1440 and page == 'index') or (width == 390 and page == 'max-independent-set'):
                    shot = ws.call('Page.captureScreenshot', {'format': 'png'})
                    (Path(tempfile.gettempdir()) / f'quanifi-{page}-{width}.png').write_bytes(base64.b64decode(shot['data']))
                records.append({'page': page + '.html', 'width': width, **state,
                                'external_http_blocked': True, 'expanded_configuration_no_overflow': True})
        (ROOT / 'evidence/browser-checks.json').write_text(json.dumps(records, indent=2) + '\n')
        print('Five pages passed desktop/mobile offline checks; both interactive examples passed.')
    finally:
        proc.terminate()
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == '__main__':
    main()
