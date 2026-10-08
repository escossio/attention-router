"""Boot controller contracts. These tests never contact the live containers."""
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / 'ops/whatsapp-container'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


boot = load('whatsapp_boot', ROOT / 'boot/bootstrap.py')
profile_install = load('whatsapp_profile_install', ROOT / 'boot/install_profile.py')


def test_profile_install_is_idempotent_and_matches_versioned_bytes(tmp_path):
    source = ROOT / 'andy-whatsapp-browser.apparmor'
    target = tmp_path / 'andy-whatsapp-browser'
    first = profile_install.install_profile(source, target, load=False, owner=os.getuid())
    mtime = target.stat().st_mtime_ns
    second = profile_install.install_profile(source, target, load=False, owner=os.getuid())
    assert first == second and target.read_bytes() == source.read_bytes()
    assert target.stat().st_mtime_ns == mtime and target.stat().st_mode & 0o777 == 0o644


def ready_preflight(monkeypatch, tmp_path):
    profile = tmp_path / 'profile.apparmor'
    profile.write_bytes(b'profile')
    install = tmp_path / 'install'
    install.mkdir()
    (install / profile.name).write_bytes(b'profile')
    (install / 'compose.yaml').write_text('services: {}')
    mounted = tmp_path / 'browser-profile'
    mounted.mkdir()
    (mounted / '.andy-browser-writer.lock').touch()
    env_file = tmp_path / 'compose.env'
    env_file.write_text('WHATSAPP_BROWSER_PARENT=enp1s0.210\nWHATSAPP_TRANSPORT_PARENT=enp1s0.211\nWHATSAPP_PROFILE_DIR=' + str(mounted))
    monkeypatch.setattr(boot, 'PROFILE', profile)
    monkeypatch.setattr(boot, 'INSTALL', install)
    monkeypatch.setattr(boot, 'run', lambda *a, **k: 'active' if a[:2] == ('systemctl', 'is-active') else '')
    monkeypatch.setattr(boot, 'loaded_profile', lambda: True)
    monkeypatch.setattr(boot, 'is_masked', lambda unit: True)
    monkeypatch.setattr(boot, 'parent_up', lambda parent: True)
    monkeypatch.setattr(boot, 'private_env_path', lambda item: env_file)
    monkeypatch.setattr(boot, 'mount_source', lambda item, dest: mounted)
    def fake_inspect(role):
        return {'Config': {'Labels': {}}, 'Mounts': [], 'AppArmorProfile': 'andy-whatsapp-browser',
                'HostConfig': {'RestartPolicy': {'Name': 'on-failure', 'MaximumRetryCount': 5}, 'ReadonlyRootfs': role == 'transport',
                               'Privileged': False, 'PortBindings': {},
                               'NetworkMode': 'container:browser' if role == 'observer' else 'default'}}
    monkeypatch.setattr(boot, 'inspect', fake_inspect)
    return profile


def test_preflight_accepts_synthetic_ready_state(monkeypatch, tmp_path):
    ready_preflight(monkeypatch, tmp_path)
    assert boot.preflight()[0]['WHATSAPP_BROWSER_PARENT'] == 'enp1s0.210'


def test_preflight_fails_if_profile_missing(monkeypatch, tmp_path):
    profile = ready_preflight(monkeypatch, tmp_path)
    profile.unlink()
    with pytest.raises(boot.BootError, match='APPARMOR_PROFILE_MISSING'):
        boot.preflight()


def test_preflight_fails_if_profile_not_loaded(monkeypatch, tmp_path):
    ready_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(boot, 'loaded_profile', lambda: False)
    with pytest.raises(boot.BootError, match='APPARMOR_PROFILE_NOT_LOADED'):
        boot.preflight()


def test_preflight_fails_if_vlan_parent_missing(monkeypatch, tmp_path):
    ready_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(boot, 'parent_up', lambda parent: parent != 'enp1s0.211')
    with pytest.raises(boot.BootError, match='VLAN_PARENT_NOT_UP'):
        boot.preflight()


def test_preflight_fails_if_legacy_unmasked(monkeypatch, tmp_path):
    ready_preflight(monkeypatch, tmp_path)
    monkeypatch.setattr(boot, 'is_masked', lambda unit: False)
    with pytest.raises(boot.BootError, match='LEGACY_NOT_MASKED'):
        boot.preflight()


def test_preflight_fails_if_restart_policy_can_race(monkeypatch, tmp_path):
    ready_preflight(monkeypatch, tmp_path)
    original = boot.inspect
    def wrong_policy(role):
        item = original(role)
        if role == 'observer':
            item['HostConfig']['RestartPolicy']['Name'] = 'unless-stopped'
        return item
    monkeypatch.setattr(boot, 'inspect', wrong_policy)
    with pytest.raises(boot.BootError, match='RESTART_POLICY_INVALID'):
        boot.preflight()


def test_start_order_and_health_gates(monkeypatch):
    steps = []
    monkeypatch.setattr(boot, 'preflight', lambda: ({}, Path('/profile')))
    monkeypatch.setattr(boot, 'start_if_stopped', lambda role: steps.append('start:' + role))
    monkeypatch.setattr(boot, 'wait_healthy', lambda role: steps.append('healthy:' + role))
    monkeypatch.setattr(boot, 'browser_gate', lambda *a: steps.append('gate:browser'))
    monkeypatch.setattr(boot, 'transport_gate', lambda *a: steps.append('gate:transport'))
    monkeypatch.setattr(boot, 'observer_gate', lambda: steps.append('gate:observer'))
    boot.converge()
    assert steps == ['start:browser', 'healthy:browser', 'gate:browser',
                     'start:transport', 'healthy:transport', 'gate:transport',
                     'start:observer', 'healthy:observer', 'gate:observer']


def test_browser_failure_blocks_dependents(monkeypatch):
    started = []
    monkeypatch.setattr(boot, 'preflight', lambda: ({}, Path('/profile')))
    monkeypatch.setattr(boot, 'start_if_stopped', lambda role: started.append(role))
    monkeypatch.setattr(boot, 'wait_healthy', lambda role: (_ for _ in ()).throw(boot.BootError('BROWSER_FAILED')))
    with pytest.raises(boot.BootError, match='BROWSER_FAILED'):
        boot.converge()
    assert started == ['browser']


def test_start_existing_container_is_idempotent(monkeypatch):
    monkeypatch.setattr(boot, 'state', lambda role: {'Running': True})
    monkeypatch.setattr(boot, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected mutation')))
    for _ in range(2):
        for role in boot.NAMES:
            boot.start_if_stopped(role)


def test_shutdown_order(monkeypatch, tmp_path):
    monkeypatch.setattr(boot, 'SKIP_STOP', tmp_path / 'absent')
    monkeypatch.setattr(boot, 'state', lambda role: {'Running': True})
    stopped = []
    monkeypatch.setattr(boot, 'run', lambda *a, **k: stopped.append(a[-1]))
    monkeypatch.setattr(boot, 'state', lambda role: {'Running': role not in {name.replace('andy-whatsapp-', '') for name in stopped}})
    boot.stop()
    assert stopped == [boot.NAMES[role] for role in ('observer', 'transport', 'browser')]


def test_rollback_skips_shutdown(monkeypatch, tmp_path):
    marker = tmp_path / 'skip'
    marker.touch()
    monkeypatch.setattr(boot, 'SKIP_STOP', marker)
    monkeypatch.setattr(boot, 'state', lambda role: (_ for _ in ()).throw(AssertionError('must not inspect')))
    boot.stop()


def test_no_pairing_logout_outbound_or_mikrotik_mutation():
    source = (ROOT / 'boot/bootstrap.py').read_text().split('\"\"\"', 2)[-1]
    for forbidden in ('pairing', 'logout', 'send_message', 'mikrotik', 'ssh ', 'docker run', 'docker compose up'):
        assert forbidden not in source.lower()


def test_no_host_cdp_port_and_sandbox_preserved():
    compose = (ROOT / 'compose.yaml').read_text()
    assert 'ports:' not in compose and 'apparmor:andy-whatsapp-browser' in compose
    assert 'seccomp:${WHATSAPP_CHROME_SECCOMP_PROFILE' in compose
    for forbidden in ('privileged:', 'SYS_ADMIN', 'no-sandbox', 'seccomp=unconfined'):
        assert forbidden not in compose
    assert compose.count('restart: "on-failure:5"') == 3


def test_preflight_rejects_profile_checksum_drift(monkeypatch, tmp_path):
    profile = ready_preflight(monkeypatch, tmp_path)
    profile.write_bytes(b'changed')
    with pytest.raises(boot.BootError, match='APPARMOR_PROFILE_MISMATCH'):
        boot.preflight()


def test_profile_installer_reloads_and_verifies_loaded_state(monkeypatch, tmp_path):
    source = ROOT / 'andy-whatsapp-browser.apparmor'
    target = tmp_path / 'andy-whatsapp-browser'
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args[0])
        class Result:
            stdout = '{"profiles":{"andy-whatsapp-browser":"enforce"}}'
        return Result()
    monkeypatch.setattr(profile_install.subprocess, 'run', fake_run)
    profile_install.install_profile(source, target, load=True, owner=os.getuid())
    assert calls == ['apparmor_parser', 'aa-status']
