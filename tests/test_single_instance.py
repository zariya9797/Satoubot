import subprocess
import sys
from unittest.mock import Mock

import pytest

from satoubot import app
from satoubot.sessions import single_instance


def test_second_process_is_blocked_and_lock_is_released(tmp_path):
    command = [sys.executable, '-c',
               'from pathlib import Path; from satoubot.sessions import single_instance; '
               'import sys; '
               '\nwith single_instance(Path(sys.argv[1])): pass', str(tmp_path)]
    with single_instance(tmp_path):
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert 'already running' in result.stderr
    # A persistent lock file must not prevent a later restart.
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_duplicate_start_exits_before_opening_databases(tmp_path, monkeypatch):
    for key, value in {'DATA_DIR': str(tmp_path), 'BOT_TOKEN': 'test',
                       'ADMIN_USER_ID': '42', 'TELEGRAM_API_ID': '123',
                       'TELEGRAM_API_HASH': 'test'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(app, 'load_dotenv', lambda: None)
    run = Mock()
    monkeypatch.setattr(app, 'run_bot', run)
    with single_instance(tmp_path):
        with pytest.raises(SystemExit, match='already running'):
            app.main()
    run.assert_not_called()
    assert not (tmp_path / 'satoubot.sqlite3').exists()
    app.main()
    run.assert_called_once()
