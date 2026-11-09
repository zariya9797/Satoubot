import socket
import threading
import time

import pytest

from satoubot.wait_bot_api import wait_for_api


def test_wait_connects_to_configured_api(monkeypatch, capsys):
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        port = server.getsockname()[1]
        monkeypatch.setenv('BOT_API_URL', f'http://secret:password@127.0.0.1:{port}/')
        assert wait_for_api(timeout=1, interval=.01)
    output = capsys.readouterr().out
    assert 'accepting connections' in output
    assert 'secret' not in output and 'password' not in output


def test_unavailable_api_fails_bounded(monkeypatch):
    with socket.socket() as unused:
        unused.bind(('127.0.0.1', 0))
        monkeypatch.setenv('BOT_API_URL', f'http://127.0.0.1:{unused.getsockname()[1]}')
        assert not wait_for_api(timeout=.05, interval=.01)


def test_invalid_port_fails_without_exposing_url(monkeypatch, capsys):
    monkeypatch.setenv('BOT_API_URL', 'http://secret:password@example.com:bad')
    assert not wait_for_api(timeout=.1)
    assert 'secret' not in capsys.readouterr().out


def test_installer_preserves_api_and_backs_up_bot_unit(tmp_path):
    """Exercise the installer with isolated unit paths and fake service tools."""
    import os
    import subprocess
    from pathlib import Path
    repo = tmp_path/'checkout'; scripts = repo/'scripts'; scripts.mkdir(parents=True)
    (repo/'.venv'/'bin').mkdir(parents=True)
    python = repo/'.venv'/'bin'/'python'; python.write_text('#!/bin/sh\nexit 0\n'); python.chmod(0o755)
    (repo/'.env').write_text('BOT_API_URL=http://127.0.0.1:8081\n')
    units = tmp_path/'units'; units.mkdir()
    (units/'satoubot.service').write_text('old unit\n')
    api = units/'satoubot-api.service'; api.write_text('[Service]\nExecStart=/bin/true\n')
    script = Path('scripts/install-systemd.sh').read_text()
    # Unit path substitution and effective-UID check keep the test unprivileged.
    script = script.replace('${EUID} -ne 0 || ', '').replace('/etc/systemd/system',str(units))
    (scripts/'install-systemd.sh').write_text(script)
    mock_bin = tmp_path/'bin'; mock_bin.mkdir()
    calls = tmp_path/'calls'
    (mock_bin/'systemctl').write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n')
    (mock_bin/'runuser').write_text('#!/bin/sh\nexit 0\n')
    for tool in mock_bin.iterdir(): tool.chmod(0o755)
    env = {**os.environ,'PATH':str(mock_bin)+':'+os.environ['PATH'],'SUDO_USER':'alice'}
    result = subprocess.run(['bash', str(scripts/'install-systemd.sh')],cwd=repo,env=env,capture_output=True,text=True,check=True)
    content = (units/'satoubot.service').read_text()
    assert 'Requires=satoubot-api.service' in content and 'After=network-online.target satoubot-api.service' in content
    assert f'WorkingDirectory={repo}' in content
    assert f'ExecStartPre={python} -m satoubot.wait_bot_api' in content
    assert list(units.glob('satoubot.service.backup.*'))[0].read_text() == 'old unit\n'
    assert api.read_text() == '[Service]\nExecStart=/bin/true\n'
    assert calls.read_text().splitlines() == ['cat satoubot-api.service --no-pager','daemon-reload']
    assert 'restart satoubot.service' in result.stdout
    import shutil
    if shutil.which('systemd-analyze'):
        subprocess.run(['systemd-analyze','verify',str(units/'satoubot.service'),str(api)],check=True,capture_output=True)


def test_wait_until_api_starts(monkeypatch):
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        monkeypatch.setenv('BOT_API_URL', f'http://127.0.0.1:{port}')
        def start():
            time.sleep(.05)
            server.listen()
        worker = threading.Thread(target=start)
        worker.start()
        try:
            assert wait_for_api(timeout=2, interval=.01)
        finally:
            worker.join()
