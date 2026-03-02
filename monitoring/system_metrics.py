import time


def get_cpu_percent() -> float:
    """讀取 /proc/stat 計算 CPU 使用率（需兩次採樣）"""
    def read_cpu_times():
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        parts = line.split()
        user, nice, system, idle, iowait = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
        total = user + nice + system + idle + iowait
        return total - idle, total

    active1, total1 = read_cpu_times()
    time.sleep(0.2)
    active2, total2 = read_cpu_times()
    delta_total = total2 - total1
    if delta_total == 0:
        return 0.0
    return round((active2 - active1) / delta_total * 100, 1)


def get_ram_percent() -> float:
    """讀取 /proc/meminfo 計算 RAM 使用率"""
    mem = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(':')] = int(parts[1])
    total = mem.get('MemTotal', 1)
    available = mem.get('MemAvailable', 0)
    used = total - available
    return round(used / total * 100, 1)


def get_disk_percent(path: str = '/') -> float:
    """使用 os.statvfs 計算磁碟使用率"""
    import os
    stat = os.statvfs(path)
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bfree * stat.f_frsize
    used = total - free
    if total == 0:
        return 0.0
    return round(used / total * 100, 1)
