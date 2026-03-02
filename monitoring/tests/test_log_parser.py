import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from monitoring.log_parser import parse_log_lines, extract_daily_stats

SAMPLE_LOGS = [
    "2026-03-02 10:00:01 INFO ✅ 処理成功: 山田商店 - 合計 ¥5,500",
    "2026-03-02 10:01:00 INFO ✅ 処理成功: ABC株式会社 - 合計 ¥12,000",
    "2026-03-02 10:02:00 ERROR ❌ 処理失敗: ファイル読み込みエラー",
    "2026-03-02 10:03:00 INFO 監視中...",
    "2026-03-02 10:04:00 WARNING ⚠️ リトライ中",
]

def test_parse_log_lines_returns_structured():
    result = parse_log_lines(SAMPLE_LOGS)
    assert len(result) == 5
    assert result[0]['level'] == 'INFO'
    assert result[2]['level'] == 'ERROR'

def test_extract_daily_stats_counts_success():
    stats = extract_daily_stats(SAMPLE_LOGS)
    assert stats['success_count'] == 2
    assert stats['fail_count'] == 1

def test_extract_daily_stats_totals_amount():
    stats = extract_daily_stats(SAMPLE_LOGS)
    assert stats['total_amount_jpy'] == 17500

def test_parse_log_lines_empty():
    result = parse_log_lines([])
    assert result == []
