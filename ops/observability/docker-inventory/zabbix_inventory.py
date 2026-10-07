#!/usr/bin/env python3
"""Inventory Docker control-plane objects through the allowlisted Unix proxy."""
import datetime as dt
import http.client
import ipaddress
import json
import re
import socket
import sys
from urllib.parse import quote

SOCKET = '/run/andy-zabbix-docker/docker.sock'


class Connection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect(SOCKET)


def get(path):
    conn = Connection()
    try:
        conn.request('GET', path)
        response = conn.getresponse()
        if response.status != 200:
            raise RuntimeError(f'Docker API {path}: HTTP {response.status}')
        return json.load(response)
    finally:
        conn.close()


def iso_uptime(started):
    try:
        start = dt.datetime.fromisoformat(started.replace('Z', '+00:00'))
        return max(0, int((dt.datetime.now(dt.timezone.utc) - start).total_seconds()))
    except (ValueError, TypeError, AttributeError):
        return 0


def matching_ipam(network, ip):
    for config in network.get('ipam') or (network.get('IPAM') or {}).get('Config') or []:
        subnet = config.get('Subnet') or ''
        try:
            if ip and subnet and ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False):
                return config
        except ValueError:
            pass
    return {}


def entity_identity(labels, name):
    project = labels.get('com.docker.compose.project') or ''
    service = labels.get('com.docker.compose.service') or ''
    ordinal = labels.get('com.docker.compose.container-number') or ''
    return f'{project}/{service}/{ordinal or "1"}' if project and service else name


def inventory():
    image_digests = {image.get('Id'): image.get('RepoDigests') or []
                     for image in get('/images/json?all=1')}
    networks = []
    for entry in get('/networks'):
        n = get('/networks/' + quote(entry['Id'], safe=''))
        ipam = (n.get('IPAM') or {}).get('Config') or []
        networks.append({
            'network_key': n['Name'], 'network_id': n['Id'], 'name': n['Name'],
            'driver': n.get('Driver') or '', 'scope': n.get('Scope') or '',
            'ipam': ipam, 'subnets': [x.get('Subnet') for x in ipam if x.get('Subnet')],
            'gateways': [x.get('Gateway') for x in ipam if x.get('Gateway')],
            'internal': bool(n.get('Internal')), 'attachable': bool(n.get('Attachable')),
            'ingress': bool(n.get('Ingress')), 'connected_count': len(n.get('Containers') or {}),
        })
    by_id = {n['network_id']: n for n in networks}
    containers, container_labels, attachments, ports = [], [], [], []
    for entry in get('/containers/json?all=1'):
        c = get('/containers/' + entry['Id'] + '/json')
        labels = (c.get('Config') or {}).get('Labels') or {}
        project = labels.get('com.docker.compose.project') or ''
        service = labels.get('com.docker.compose.service') or ''
        ordinal = labels.get('com.docker.compose.container-number') or ''
        name = c.get('Name', '').lstrip('/')
        entity_key = entity_identity(labels, name)
        state = c.get('State') or {}
        host_config = c.get('HostConfig') or {}
        network_settings = c.get('NetworkSettings') or {}
        observed_networks = network_settings.get('Networks') or {}
        network_names = sorted(observed_networks)
        network_drivers = sorted({(by_id.get(link.get('NetworkID')) or {}).get('driver') or ''
                                  for link in observed_networks.values()} - {''})
        facts = {
            'entity_key': entity_key, 'container_id': c['Id'], 'instance_id': c['Id'],
            'name': name, 'service': service or name, 'compose_project': project,
            'compose_service': service, 'compose_number': ordinal, 'state': state.get('Status') or '',
            'health': (state.get('Health') or {}).get('Status') or '',
            'image': (c.get('Config') or {}).get('Image') or '', 'image_id': c.get('Image') or '',
            'image_digests': image_digests.get(c.get('Image')) or [],
            'created': c.get('Created') or '', 'restart_count': c.get('RestartCount') or 0,
            'restart_policy': (host_config.get('RestartPolicy') or {}).get('Name') or '',
            'uptime_seconds': iso_uptime(state.get('StartedAt')) if state.get('Running') else 0,
            'networks': network_names, 'networks_tag': ','.join(network_names),
            'network_drivers_tag': ','.join(network_drivers),
            'service_name': labels.get('com.docker.compose.service') or name,
            'deployment_environment': labels.get('deployment.environment') or '',
            'host_name': 'agt01',
        }
        containers.append(facts)
        container_labels.append({'entity_key': entity_key, 'container_id': c['Id'],
                                 'labels': labels})
        for network_name, link in observed_networks.items():
            network = by_id.get(link.get('NetworkID')) or next((n for n in networks if n['name'] == network_name), {})
            ip = link.get('IPAddress') or ''
            ipv6 = link.get('GlobalIPv6Address') or ''
            config = matching_ipam(network, ip)
            config6 = matching_ipam(network, ipv6)
            attachments.append({
                'attachment_key': entity_key + '@' + network_name,
                'entity_key': entity_key, 'container_id': c['Id'], 'container_name': name,
                'network': network_name, 'network_id': network.get('network_id') or '',
                'driver': network.get('driver') or '', 'ip': ip, 'gateway': link.get('Gateway') or config.get('Gateway') or '',
                'subnet': config.get('Subnet') or '', 'mac': link.get('MacAddress') or '',
                'ipv6': ipv6, 'ipv6_gateway': link.get('IPv6Gateway') or config6.get('Gateway') or '',
                'ipv6_subnet': config6.get('Subnet') or '',
            })
        exposed = (c.get('Config') or {}).get('ExposedPorts') or {}
        published = network_settings.get('Ports') or {}
        declared = host_config.get('PortBindings') or {}
        for spec in sorted(set(exposed) | set(published) | set(declared)):
            match = re.fullmatch(r'(\d+)/(tcp|udp|sctp)', spec)
            if not match:
                continue
            binds = published.get(spec) or declared.get(spec) or []
            for bind in binds or [None]:
                host_ip = bind.get('HostIp') or '' if bind else ''
                host_port = bind.get('HostPort') or '' if bind else ''
                ports.append({
                    'port_key': entity_key + '@' + spec + '@' + host_ip + ':' + host_port,
                    'entity_key': entity_key, 'container_id': c['Id'], 'container_name': name,
                    'private_port': int(match.group(1)), 'protocol': match.group(2),
                    'exposed': spec in exposed, 'published': bool(bind),
                    'bind_address': host_ip, 'public_port': int(host_port) if host_port else '',
                })
    return {'collected_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'containers': containers, 'container_labels': container_labels,
            'networks': networks, 'attachments': attachments, 'ports': ports}


if __name__ == '__main__':
    try:
        result = inventory()
        section = sys.argv[1] if len(sys.argv) > 1 else ''
        if section and section not in {'containers', 'container_labels', 'networks', 'attachments', 'ports'}:
            raise ValueError('Invalid inventory section')
        print(json.dumps(result[section] if section else result, separators=(',', ':')))
    except Exception as exc:
        print(f'docker inventory: {exc}', file=sys.stderr)
        sys.exit(1)
