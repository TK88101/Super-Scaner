import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from monitoring.system_metrics import get_cpu_percent, get_ram_percent, get_disk_percent

def test_cpu_percent_returns_float():
    result = get_cpu_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0

def test_ram_percent_returns_float():
    result = get_ram_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0

def test_disk_percent_returns_float():
    result = get_disk_percent()
    assert isinstance(result, float)
    assert 0.0 <= result <= 100.0
