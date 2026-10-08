#!/usr/bin/env python3
"""Fail-closed boot and shutdown of the existing WhatsApp containers.

No compose up/build, container recreation, profile rewrite, pairing or outbound API.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse, urlunparse
from urllib.request import urlopen

INSTALL = Path('/usr/local/lib/andy-whatsapp-container-runtime')
PROFILE = Path('/etc/apparmor.d/andy-whatsapp-browser')
SKIP_STOP = Path('/run/andy-whatsapp-container-runtime.skip-stop')
NAMES = {'browser': 'andy-whatsapp-browser', 'transport': 'andy-whatsapp-transport',
         'observer': 'andy-whatsapp-observer'}
LEGACY = (
    'attention-whatsapp-transport.service', 'attention-whatsapp-browser.service',
    'attention-whatsapp-xvfb.service', 'attention-whatsapp-observer.service',
    'andy-browser-cdp-edge.service', 'andy-transport-cdp-proxy.service',
    'andy-browser-netns.service', 'andy-transport-netns.service',
)


class BootError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise BootError(code)


def run(*args: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BootError(f'COMMAND_UNAVAILABLE:{args[0]}') from error
    if result.returncode:
        raise BootError(f'COMMAND_FAILED:{args[0]}')
    return result.stdout


def inspect(role: str) -> dict:
    payload = json.loads(run('docker', 'inspect', NAMES[role]))
    require(len(payload) == 1, f'{role.upper()}_CONTAINER_MISSING')
    return payload[0]


def state(role: str) -> dict:
    return inspect(role).get('State') or {}


def loaded_profile() -> bool:
    payload = json.loads(run('aa-status', '--json'))
    return payload.get('profiles', {}).get('andy-whatsapp-browser') == 'enforce'


def private_env_path(browser: dict) -> Path:
    labels = (browser.get('Config') or {}).get('Labels') or {}
    value = labels.get('com.docker.compose.project.environment_file') or ''
    require(value.startswith('/') and '\n' not in value, 'COMPOSE_ENV_LABEL_INVALID')
    return Path(value)


def read_env(path: Path) -> dict[str, str]:
    require(path.is_file(), 'COMPOSE_ENV_MISSING')
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip().strip('"\'')
    return values


def mount_source(item: dict, destination: str) -> Path:
    mounts = [m for m in item.get('Mounts') or [] if m.get('Destination') == destination and m.get('Type') == 'bind']
    require(len(mounts) == 1, f'MOUNT_MISSING:{destination}')
    source = Path(mounts[0]['Source'])
    require(source.is_dir(), f'MOUNT_SOURCE_MISSING:{destination}')
    return source


def is_masked(unit: str) -> bool:
    result = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True, timeout=3)
    return result.stdout.strip() == 'masked'


def parent_up(parent: str) -> bool:
    operstate = Path('/sys/class/net') / parent / 'operstate'
    return operstate.is_file() and operstate.read_text().strip() == 'up'


def preflight() -> tuple[dict[str, str], Path]:
    require(run('systemctl', 'is-active', 'docker.service').strip() == 'active', 'DOCKER_INACTIVE')
    require(run('systemctl', 'is-active', 'apparmor.service').strip() == 'active', 'APPARMOR_SERVICE_INACTIVE')
    bundled_profile = INSTALL / 'andy-whatsapp-browser.apparmor'
    require(PROFILE.is_file() and bundled_profile.is_file(), 'APPARMOR_PROFILE_MISSING')
    require(hashlib.sha256(PROFILE.read_bytes()).digest() ==
            hashlib.sha256(bundled_profile.read_bytes()).digest(), 'APPARMOR_PROFILE_MISMATCH')
    require(loaded_profile(), 'APPARMOR_PROFILE_NOT_LOADED')
    for unit in LEGACY:
        require(is_masked(unit), f'LEGACY_NOT_MASKED:{unit}')
    browser = inspect('browser')
    transport = inspect('transport')
    observer = inspect('observer')
    env_path = private_env_path(browser)
    env = read_env(env_path)
    parents = {'browser': env.get('WHATSAPP_BROWSER_PARENT') or '',
               'transport': env.get('WHATSAPP_TRANSPORT_PARENT') or ''}
    require(parents['browser'].endswith('.210') and parents['transport'].endswith('.211'),
            'VLAN_PARENT_CONFIG_MISMATCH')
    for role, item in (('browser', browser), ('transport', transport)):
        parent = parents[role]
        require(parent_up(parent), f'VLAN_PARENT_NOT_UP:{role}')
        networks = (item.get('NetworkSettings') or {}).get('Networks') or {}
        wire = [name for name in networks if name.endswith(f'_{role}_wire')]
        require(len(wire) == 1, f'VLAN_DOCKER_NETWORK_MISSING:{role}')
        network = json.loads(run('docker', 'network', 'inspect', wire[0]))[0]
        require((network.get('Options') or {}).get('parent') == parent,
                f'VLAN_DOCKER_PARENT_MISMATCH:{role}')
    profile = mount_source(browser, '/profile')
    require(profile == Path(env.get('WHATSAPP_PROFILE_DIR', '')), 'PROFILE_MOUNT_MISMATCH')
    require((profile / '.andy-browser-writer.lock').is_file(), 'PROFILE_WRITER_GUARD_MISSING')
    mount_source(transport, '/var/lib/attention-router/whatsapp-transport')
    mount_source(transport, '/var/lib/attention-router/whatsapp-media')
    mount_source(observer, '/var/lib/attention-router/whatsapp-observer')
    require((transport.get('HostConfig') or {}).get('ReadonlyRootfs') is True, 'TRANSPORT_ROOTFS_NOT_READ_ONLY')
    for role, item in (('browser', browser), ('transport', transport), ('observer', observer)):
        policy = (item.get('HostConfig') or {}).get('RestartPolicy') or {}
        require(policy.get('Name') == 'on-failure' and policy.get('MaximumRetryCount') == 5,
                f'RESTART_POLICY_INVALID:{role}')
    require(browser.get('AppArmorProfile') == 'andy-whatsapp-browser', 'BROWSER_APPARMOR_MISMATCH')
    require(not (browser.get('HostConfig') or {}).get('Privileged'), 'BROWSER_PRIVILEGED')
    require(not (transport.get('HostConfig') or {}).get('Privileged'), 'TRANSPORT_PRIVILEGED')
    require(not (observer.get('HostConfig') or {}).get('Privileged'), 'OBSERVER_PRIVILEGED')
    for item in (browser, transport, observer):
        require(not (item.get('HostConfig') or {}).get('PortBindings'), 'HOST_PORT_EXPOSED')
    require((observer.get('HostConfig') or {}).get('NetworkMode', '').startswith('container:'),
            'OBSERVER_BROWSER_NAMESPACE_MISMATCH')
    run('docker', 'compose', '--env-file', str(env_path), '-f', str(INSTALL / 'compose.yaml'),
        '--profile', 'production', 'config', '--quiet', timeout=20)
    return env, profile


def wait_healthy(role: str, seconds: int = 150) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = state(role)
        if current.get('Running') and (current.get('Health') or {}).get('Status') == 'healthy':
            return
        if current.get('Status') in ('dead', 'removing'):
            break
        time.sleep(3)
    raise BootError(f'{role.upper()}_HEALTH_TIMEOUT')


def start_if_stopped(role: str) -> None:
    if not state(role).get('Running'):
        run('docker', 'start', NAMES[role], timeout=45)
        print(f'BOOT_START={role}', flush=True)


def check_ip(item: dict, expected: str) -> None:
    networks = (item.get('NetworkSettings') or {}).get('Networks') or {}
    require(any(value.get('IPAddress') == expected for value in networks.values()), 'CONTAINER_IP_MISMATCH')


def check_forwarding(role: str) -> None:
    require(run('docker', 'exec', NAMES[role], 'cat', '/proc/sys/net/ipv4/ip_forward').strip() == '0',
            f'{role.upper()}_IP_FORWARD_ENABLED')



def browser_auth(item: dict, page: dict) -> None:
    """Read only the current state via the existing private Docker CDP bridge."""
    try:
        import websocket
    except ImportError as error:
        raise BootError('WEBSOCKET_CLIENT_MISSING') from error
    networks = (item.get('NetworkSettings') or {}).get('Networks') or {}
    cdp = [(name, value) for name, value in networks.items() if name.endswith('_cdp')]
    require(len(cdp) == 1, 'PRIVATE_CDP_NETWORK_MISSING')
    network = json.loads(run('docker', 'network', 'inspect', cdp[0][0]))[0]
    require(network.get('Internal') is True, 'CDP_NETWORK_NOT_PRIVATE')
    ip = cdp[0][1].get('IPAddress') or ''
    require(ip.startswith('192.168.') or ip.startswith('172.'), 'PRIVATE_CDP_IP_MISSING')
    original = urlparse(page.get('webSocketDebuggerUrl') or '')
    require(original.scheme == 'ws' and original.hostname == '127.0.0.1' and
            original.path.startswith('/devtools/page/'), 'PRIVATE_CDP_TARGET_INVALID')
    target = urlunparse(original._replace(netloc=f'{ip}:{original.port}'))
    connection = websocket.create_connection(target, timeout=3, suppress_origin=True)
    try:
        expression = "({appState:window.AuthStore?.AppState?.state||null,nativeState:window.require?.('WAWebSocketModel')?.Socket?.state||null})"
        connection.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                                    'params': {'expression': expression, 'returnByValue': True}}))
        for _ in range(10):
            message = json.loads(connection.recv())
            if message.get('id') == 1:
                value = message.get('result', {}).get('result', {}).get('value', {})
                require(value.get('appState') == 'CONNECTED' and value.get('nativeState') == 'CONNECTED',
                        'BROWSER_AUTH_NOT_CONNECTED')
                return
        raise BootError('BROWSER_AUTH_PROBE_TIMEOUT')
    finally:
        connection.close()


def browser_gate(env: dict[str, str], profile: Path) -> None:
    item = inspect('browser')
    require(item['State']['Running'] and item['State']['Health']['Status'] == 'healthy', 'BROWSER_NOT_HEALTHY')
    restart_count = item.get('RestartCount')
    check_ip(item, env['WHATSAPP_BROWSER_IP'])
    check_forwarding('browser')
    targets = json.loads(run('docker', 'exec', NAMES['browser'], 'curl', '-fsS',
                             'http://127.0.0.1:9223/json/list'))
    pages = [target for target in targets if target.get('type') == 'page' and
             str(target.get('url') or '').startswith('https://web.whatsapp.com/')]
    require(len(pages) == 1, 'BROWSER_PAGE_TOPOLOGY_INVALID')
    browser_auth(item, pages[0])
    writers = [line for line in run('docker', 'exec', NAMES['browser'], 'ps', '-eo', 'args').splitlines()
               if line.startswith('/usr/bin/google-chrome ') and '--user-data-dir=/profile ' in line]
    require(len(writers) == 1, 'BROWSER_WRITER_COUNT_INVALID')
    with (profile / '.andy-browser-writer.lock').open('rb') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            raise BootError('BROWSER_OPERATIONAL_FLOCK_NOT_HELD')
    log_result = subprocess.run(['docker', 'logs', '--since', item['State']['StartedAt'],
                                 NAMES['browser']], capture_output=True, text=True, timeout=15)
    require(log_result.returncode == 0, 'BROWSER_LOG_UNAVAILABLE')
    logs = log_result.stdout + log_result.stderr
    require(any(marker in logs for marker in ('SINGLETON_STATE=CLEAN', 'SINGLETON_STATE=STALE_REMOVED',
                                              'SINGLETON_STATE=ACTIVE')), 'BROWSER_SINGLETON_STATE_UNKNOWN')
    require(inspect('browser').get('RestartCount') == restart_count, 'BROWSER_RESTART_COUNT_CHANGED')


def transport_gate(env: dict[str, str]) -> None:
    item = inspect('transport')
    require(item['State']['Running'] and item['State']['Health']['Status'] == 'healthy', 'TRANSPORT_NOT_HEALTHY')
    check_ip(item, env['WHATSAPP_TRANSPORT_IP'])
    check_forwarding('transport')
    run('docker', 'exec', NAMES['transport'], 'test', '-w', '/var/lib/attention-router/whatsapp-transport')
    run('docker', 'exec', NAMES['transport'], 'test', '-w', '/var/lib/attention-router/whatsapp-media')
    with urlopen('http://10.77.10.10:18103/status', timeout=3) as response:
        status = json.load(response)
    require(status.get('ready') is True and status.get('client_state') == 'CONNECTED', 'TRANSPORT_NOT_CONNECTED')
    require(status.get('browser_debug_reachable') is True and
            status.get('authenticated_identity_match') is True, 'TRANSPORT_AUTH_MISMATCH')
    require(status.get('qr_seen') is False, 'QR_SEEN')
    require(status.get('disconnect_count') == 0, 'TRANSPORT_DISCONNECT_REGRESSION')
    require(status.get('page_count') == 1, 'TRANSPORT_PAGE_COUNT_INVALID')


def observer_gate() -> None:
    item = inspect('observer')
    require(item['State']['Running'] and item['State']['Health']['Status'] == 'healthy', 'OBSERVER_NOT_HEALTHY')
    output = mount_source(item, '/var/lib/attention-router/whatsapp-observer')
    status = json.loads((output / 'status.json').read_text(encoding='utf-8'))
    require(status.get('service_state') == 'READY' and status.get('browser_connected') is True,
            'OBSERVER_NOT_READY')
    require(status.get('whatsapp_page_count') == 1 and status.get('connected_page_count') == 1,
            'OBSERVER_PAGE_COUNT_INVALID')
    require(status.get('app_state') == 'CONNECTED' and status.get('capture_body') is False,
            'OBSERVER_STATE_INVALID')
    stamp = datetime.fromisoformat(status['generated_at'].replace('Z', '+00:00'))
    require((datetime.now(timezone.utc) - stamp).total_seconds() < 90, 'OBSERVER_STATUS_STALE')


def converge() -> None:
    env, profile = preflight()
    print('BOOT_PREFLIGHT=PASS', flush=True)
    start_if_stopped('browser')
    wait_healthy('browser')
    browser_gate(env, profile)
    print('BOOT_BROWSER=PASS', flush=True)
    start_if_stopped('transport')
    wait_healthy('transport')
    transport_gate(env)
    print('BOOT_TRANSPORT=PASS', flush=True)
    start_if_stopped('observer')
    wait_healthy('observer')
    observer_gate()
    print('BOOT_OBSERVER=PASS', flush=True)
    print('BOOT_CONVERGENCE=PASS', flush=True)


def stop() -> None:
    if SKIP_STOP.exists():
        print('BOOT_STOP=SKIPPED_ROLLBACK', flush=True)
        return
    for role in ('observer', 'transport', 'browser'):
        if state(role).get('Running'):
            run('docker', 'stop', '--time', '45', NAMES[role], timeout=60)
            require(not state(role).get('Running'), f'{role.upper()}_STOP_INCOMPLETE')
            print(f'BOOT_STOP={role}', flush=True)


def main() -> int:
    try:
        if sys.argv[1:] == ['preflight']:
            preflight()
            print('BOOT_PREFLIGHT=PASS', flush=True)
        elif sys.argv[1:] == ['converge']:
            converge()
        elif sys.argv[1:] == ['stop']:
            stop()
        else:
            raise BootError('USAGE:preflight|converge|stop')
    except (BootError, OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f'BOOT_RESULT=FAIL reason={str(error).split(":", 1)[0]}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
