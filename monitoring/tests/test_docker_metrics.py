import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from unittest.mock import patch, MagicMock
from monitoring.docker_metrics import get_container_status, get_container_logs

def test_get_container_status_running():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"Status":"running","RestartCount":2}'
    with patch('subprocess.run', return_value=mock_result):
        status = get_container_status('scan-bot')
    assert status['status'] == 'running'
    assert status['restart_count'] == 2

def test_get_container_status_not_found():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ''
    with patch('subprocess.run', return_value=mock_result):
        status = get_container_status('scan-bot')
    assert status['status'] == 'not_found'
    assert status['restart_count'] == 0

def test_get_container_logs_returns_list():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = 'line1\nline2\nline3'
    with patch('subprocess.run', return_value=mock_result):
        logs = get_container_logs('scan-bot', tail=100)
    assert isinstance(logs, list)
    assert len(logs) == 3
