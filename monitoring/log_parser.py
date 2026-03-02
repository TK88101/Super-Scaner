import re
from typing import List, Dict


def parse_log_lines(lines: List[str]) -> List[Dict]:
    """將日誌行解析為結構化字典"""
    result = []
    # 嘗試匹配 "YYYY-MM-DD HH:MM:SS LEVEL message" 格式
    pattern = re.compile(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s+(INFO|ERROR|WARNING|DEBUG)\s+(.*)')
    for line in lines:
        m = pattern.match(line)
        if m:
            result.append({
                'timestamp': m.group(1),
                'level': m.group(2),
                'message': m.group(3)
            })
        else:
            # 無法解析時歸類為 INFO
            result.append({
                'timestamp': '',
                'level': 'INFO',
                'message': line
            })
    return result


def extract_daily_stats(lines: List[str]) -> Dict:
    """從日誌行中提取當日統計（成功數、失敗數、總金額）"""
    success_count = 0
    fail_count = 0
    total_amount = 0

    success_pattern = re.compile(r'✅.*処理成功')
    fail_pattern = re.compile(r'❌.*処理失敗')
    amount_pattern = re.compile(r'¥([\d,]+)')

    for line in lines:
        if success_pattern.search(line):
            success_count += 1
            m = amount_pattern.search(line)
            if m:
                total_amount += int(m.group(1).replace(',', ''))
        elif fail_pattern.search(line):
            fail_count += 1

    return {
        'success_count': success_count,
        'fail_count': fail_count,
        'total_amount_jpy': total_amount
    }
