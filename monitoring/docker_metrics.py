import subprocess
import json
from typing import Dict, List


def get_container_status(container_name: str) -> Dict:
    """執行 docker inspect 獲取容器狀態"""
    result = subprocess.run(
        ['docker', 'inspect', '--format',
         '{"Status":"{{.State.Status}}","RestartCount":{{.RestartCount}}}',
         container_name],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {'status': 'not_found', 'restart_count': 0}
    try:
        data = json.loads(result.stdout.strip())
        return {
            'status': data.get('Status', 'unknown'),
            'restart_count': data.get('RestartCount', 0)
        }
    except json.JSONDecodeError:
        return {'status': 'unknown', 'restart_count': 0}


def get_container_logs(container_name: str, tail: int = 100) -> List[str]:
    """抓取容器最新日誌"""
    result = subprocess.run(
        ['docker', 'logs', '--tail', str(tail), container_name],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    # docker logs 可能輸出到 stderr（正常行為）
    output = result.stdout or result.stderr
    return [line for line in output.splitlines() if line.strip()]
