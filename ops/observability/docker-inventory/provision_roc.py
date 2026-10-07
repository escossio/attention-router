#!/usr/bin/env python3
"""Import the versioned Docker template into the canonical ROC Zabbix API."""
import argparse
import json
import urllib.request
from pathlib import Path

TEMPLATE = 'AGT01 Docker control plane inventory'
RULES = {
    'template_groups': {'createMissing': False},
    'templates': {'createMissing': True, 'updateExisting': False},
    'items': {'createMissing': True, 'updateExisting': False},
    'discoveryRules': {'createMissing': True, 'updateExisting': False},
}


def api(url, method, params, token=None):
    headers = {'Content-Type': 'application/json-rpc'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(url, json.dumps({
        'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1,
    }).encode(), headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if 'error' in result:
        raise RuntimeError(f'{method}: {result["error"]["data"]}')
    return result['result']


def provision(settings, template_path):
    url = settings['api_url']
    token = settings.get('api_token') or api(url, 'user.login', {
        'username': settings['username'], 'password': settings['password'],
    })
    host_name = settings['engine_host']
    templates = api(url, 'template.get', {
        'output': ['templateid', 'host'], 'filter': {'host': [TEMPLATE]},
    }, token)
    if not templates:
        source = template_path.read_text()
        comparison = api(url, 'configuration.importcompare', {
            'format': 'yaml', 'source': source, 'rules': RULES,
        }, token)
        if comparison.get('templates', {}).get('updated') or comparison.get('templates', {}).get('removed'):
            raise RuntimeError('Template comparison includes updates or removals')
        api(url, 'configuration.import', {
            'format': 'yaml', 'source': source, 'rules': RULES,
        }, token)
        templates = api(url, 'template.get', {
            'output': ['templateid', 'host'], 'filter': {'host': [TEMPLATE]},
        }, token)
    if len(templates) != 1:
        raise RuntimeError('Expected one Docker template')
    template_id = templates[0]['templateid']
    hosts = api(url, 'host.get', {
        'output': ['hostid', 'host'], 'filter': {'host': [host_name]},
        'selectParentTemplates': ['templateid'],
    }, token)
    if not hosts:
        host_id = api(url, 'host.create', {
            'host': host_name, 'name': host_name,
            'description': 'Canonical ROC inventory of the Docker Engine via host Agent2 and the read-only Unix Docker view.',
            'groups': [{'groupid': settings['host_group_id']}],
            'interfaces': [{'type': 1, 'main': 1, 'useip': 1,
                            'ip': settings['agent_address'], 'dns': '',
                            'port': settings.get('agent_port', '10050')}],
            'templates': [{'templateid': template_id}],
            'tags': [{'tag': 'entity.type', 'value': 'engine'},
                     {'tag': 'host.name', 'value': settings['engine_name']},
                     {'tag': 'runtime', 'value': 'docker'},
                     {'tag': 'observability.stack', 'value': 'roc'}],
        }, token)['hostids'][0]
    else:
        host = hosts[0]
        host_id = host['hostid']
        if template_id not in {t['templateid'] for t in host['parentTemplates']}:
            raise RuntimeError('Existing host is not linked to Docker template; review manually')
    return {'hostid': host_id, 'templateid': template_id}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', required=True, type=Path)
    parser.add_argument('--template', type=Path,
                        default=Path(__file__).with_name('template.yaml'))
    args = parser.parse_args()
    print(json.dumps(provision(json.loads(args.settings.read_text()), args.template)))
