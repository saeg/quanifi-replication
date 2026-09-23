#!/usr/bin/env python3
"""Create supplementary examples through NiFi's REST API; never edit its live flow file.

Credentials are read from a private token file. All new processors start stopped.
The run command operates only on the process groups recorded by create.
"""
import argparse
import datetime
import json
import shutil
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NiFi:
    def __init__(self, url, token_file, insecure=False):
        self.url = url.rstrip('/') + '/nifi-api'
        self.token = Path(token_file).read_text().strip()
        self.context = ssl._create_unverified_context() if insecure else ssl.create_default_context()

    def call(self, path, data=None, method=None, raw=False):
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=120) as response:
                content = response.read()
                return content if raw else json.loads(content) if content else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'{req.method} {path}: {exc.code} {exc.read().decode()[:1500]}') from None

    def state(self, pid, state):
        current = self.call('/processors/' + pid)
        return self.call('/processors/' + pid + '/run-status', {
            'revision': current['revision'], 'state': state,
            'disconnectedNodeAcknowledged': False,
        }, 'PUT')


def save(name, data):
    (ROOT / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def create(api, output_dir):
    state_path = ROOT / 'evidence/deployment.json'
    if state_path.exists():
        raise RuntimeError('Deployment record already exists; refusing to create duplicate groups.')
    types = api.call('/flow/processor-types')['processorTypes']

    def bundle(kind):
        candidates = [t for t in types if t['type'] == kind]
        if len(candidates) != 1:
            raise RuntimeError(f'Expected exactly one installed type for {kind}: {len(candidates)}')
        return candidates[0]['bundle']

    parent = api.call('/process-groups/root')['id']
    deployment = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'groups': []}
    for index, spec in enumerate(json.loads((ROOT / 'flows/specifications.json').read_text())):
        group = api.call('/process-groups/' + parent + '/process-groups', {
            'revision': {'version': 0},
            'component': {'name': 'RQ1 — ' + spec['algorithm'],
                          'comments': spec['summary'], 'position': {'x': 80 + index * 450, 'y': 80}},
        })
        gid = group['id']
        record = {'id': spec['id'], 'group_id': gid, 'lanes': []}
        deployment['groups'].append(record)
        save('evidence/deployment.json', deployment)
        for row, lane in enumerate(spec['lanes']):
            api.call(f'/process-groups/{gid}/labels', {
                'revision': {'version': 0}, 'component': {
                    'label': lane['label'], 'position': {'x': 80, 'y': 40 + row * 420},
                    'width': (len(lane['steps']) + 1) * 650 + 350, 'height': 55,
                    'style': {'font-size': '24px', 'background-color': '#e8f0f7'},
                },
            })
            steps = [{'type': 'org.apache.nifi.processors.standard.GenerateFlowFile',
                      'name': 'One input', 'properties': {'File Size': '0B', 'Batch Size': '1',
                      'Data Format': 'Text', 'Custom Text': json.dumps(lane.get('input', {})),
                      'filename': lane['id'] + '.json'}}]
            steps += lane['steps']
            steps += [{'type': 'org.apache.nifi.processors.standard.PutFile', 'name': 'Save result JSON',
                       'properties': {'Directory': str(Path(output_dir).resolve()),
                                      'Conflict Resolution Strategy': 'replace',
                                      'Create Missing Directories': 'true'}}]
            ids, connections = [], []
            for col, step in enumerate(steps):
                config = {'properties': step['properties'], 'schedulingPeriod': '1 day' if col == 0 else '0 sec',
                          'runDurationMillis': 0, 'concurrentlySchedulableTaskCount': 1,
                          'autoTerminatedRelationships': ['success', 'failure'] if col == len(steps)-1 else []}
                proc = api.call(f'/process-groups/{gid}/processors', {
                    'revision': {'version': 0}, 'component': {
                        'name': step['name'], 'type': step['type'], 'bundle': bundle(step['type']),
                        'position': {'x': 80 + col * 650, 'y': 130 + row * 420}, 'config': config,
                    },
                })
                ids.append(proc['id'])
                if col:
                    conn = api.call(f'/process-groups/{gid}/connections', {
                        'revision': {'version': 0}, 'component': {
                            'name': 'success', 'source': {'id': ids[-2], 'groupId': gid, 'type': 'PROCESSOR'},
                            'destination': {'id': ids[-1], 'groupId': gid, 'type': 'PROCESSOR'},
                            'selectedRelationships': ['success'],
                        },
                    })
                    connections.append(conn['id'])
            # Keep failures in a visible queue instead of silently discarding them.
            failure_connections = []
            for col, pid in enumerate(ids[1:-1], 1):
                failure = api.call(f'/process-groups/{gid}/funnels', {
                    'revision': {'version': 0}, 'component': {
                        'position': {'x': 80 + col * 650 + 160, 'y': 320 + row * 420}},
                })
                conn = api.call(f'/process-groups/{gid}/connections', {
                    'revision': {'version': 0}, 'component': {
                        'name': 'failure', 'source': {'id': pid, 'groupId': gid, 'type': 'PROCESSOR'},
                        'destination': {'id': failure['id'], 'groupId': gid, 'type': 'FUNNEL'},
                        'selectedRelationships': ['failure'],
                    },
                })
                failure_connections.append(conn['id'])
            record['lanes'].append({'id': lane['id'], 'processors': ids, 'connections': connections,
                                    'failure_connections': failure_connections})
            save('evidence/deployment.json', deployment)
        print('Created', spec['id'], gid, flush=True)
    return deployment


def validation(api, deployment):
    result = []
    for group in deployment['groups']:
        for lane in group['lanes']:
            for pid in lane['processors']:
                c = api.call('/processors/' + pid)['component']
                result.append({'id': pid, 'name': c['name'], 'type': c['type'],
                               'state': c['state'], 'validationStatus': c.get('validationStatus'),
                               'validationErrors': c.get('validationErrors', [])})
    return result


def wait_until(fn, seconds=240):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError('NiFi operation did not finish within the time limit')


def queue(api, cid):
    request = api.call(f'/flowfile-queues/{cid}/listing-requests', {}, 'POST')['listingRequest']
    rid = request['id']
    try:
        def poll():
            r = api.call(f'/flowfile-queues/{cid}/listing-requests/{rid}')['listingRequest']
            return r if r['finished'] else None
        return wait_until(poll, 30).get('flowFileSummaries', [])
    finally:
        api.call(f'/flowfile-queues/{cid}/listing-requests/{rid}', method='DELETE')


def run(api, deployment):
    def all_valid():
        report = validation(api, deployment)
        save('evidence/validation.json', report)
        return report if all(p['validationStatus'] == 'VALID' for p in report) else None
    wait_until(all_valid)
    for group in deployment['groups']:
        for lane in group['lanes']:
            ids = lane['processors']
            cid = lane['connections'][-1]
            if queue(api, cid):
                raise RuntimeError('Result queue is not empty; refusing to mix runs')
            started = []
            try:
                for pid in ids[1:-1]:
                    api.state(pid, 'RUNNING')
                    started.append(pid)
                api.state(ids[0], 'RUN_ONCE')
                def finished():
                    for failure_cid in lane['failure_connections']:
                        failed = queue(api, failure_cid)
                        if failed:
                            details = api.call(f'/flowfile-queues/{failure_cid}/flowfiles/' + failed[0]['uuid'])
                            save('evidence/' + lane['id'] + '-failure.json', details)
                            raise RuntimeError(f"{lane['id']} routed to failure; see saved evidence")
                    items = queue(api, cid)
                    return items if items else None
                items = wait_until(finished, 360)
                assert len(items) == 1, 'Expected exactly one result per trigger'
                uuid = items[0]['uuid']
                detail = api.call(f'/flowfile-queues/{cid}/flowfiles/{uuid}')['flowFile']
                content = api.call(f'/flowfile-queues/{cid}/flowfiles/{uuid}/content', raw=True)
                attrs = detail['attributes']
                # Save algorithm evidence, without machine paths, credentials, or account details.
                prefixes = ('dj.', 'bv.', 'sim.', 'qaoa.', 'hamiltonian.', 'portfolio.', 'run.', 'circuit.', 'report.')
                attrs = {k: v for k, v in attrs.items() if k.startswith(prefixes)}
                evidence = {'captured_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            'flowfile_uuid': uuid, 'connection_id': cid, 'attributes': attrs,
                            'distribution': json.loads(content)}
                save('evidence/' + lane['id'] + '.json', evidence)
                (ROOT / 'evidence' / (lane['id'] + '-distribution.json')).write_bytes(content)
                api.state(ids[-1], 'RUNNING')
                started.append(ids[-1])
                wait_until(lambda: not queue(api, cid), 30)
                print('Executed', lane['id'], flush=True)
            finally:
                for pid in reversed(started):
                    api.state(pid, 'STOPPED')
        export = api.call('/process-groups/' + group['group_id'] + '/download')
        # Parameterise the sink path in the portable export, while retaining run evidence separately.
        def portable(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'Directory' and isinstance(item, str):
                        value[key] = './supplementary-results'
                    else:
                        portable(item)
            elif isinstance(value, list):
                for item in value:
                    portable(item)
        portable(export)
        save('flows/' + group['id'] + '.json', export)
    save('evidence/validation.json', validation(api, deployment))


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['create', 'validate', 'run'])
    parser.add_argument('--url', default='https://localhost:8450')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--insecure', action='store_true', help='Trust the local self-signed development certificate')
    parser.add_argument('--output-dir', default='/tmp/quanifi-supplementary-results')
    parser.add_argument('--run-dir', type=Path, help='Record a new deployment and evidence outside the archived artifact')
    args = parser.parse_args()
    if args.run_dir:
        archive = ROOT
        ROOT = args.run_dir.resolve()
        for directory in ['flows', 'evidence']:
            (ROOT / directory).mkdir(parents=True, exist_ok=True)
        if args.command == 'create' and not (ROOT / 'flows/specifications.json').exists():
            shutil.copy2(archive / 'flows/specifications.json', ROOT / 'flows/specifications.json')
    api = NiFi(args.url, args.token_file, args.insecure)
    if args.command == 'create':
        create(api, args.output_dir)
    else:
        deployment = json.loads((ROOT / 'evidence/deployment.json').read_text())
        if args.command == 'run':
            run(api, deployment)
        else:
            print(json.dumps(validation(api, deployment), indent=2))


if __name__ == '__main__':
    main()
