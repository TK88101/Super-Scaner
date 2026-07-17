"""IP-302/IP-303 監視夾入口守衛（サンデヴィスタン統合、ヘッドレスモード）。

背景：控制面（サンデヴィスタン）が交棒契約に従い、監視夾へ file を投入する前に
Drive 公開 properties へ base posting_id を書込む（契約 job-state-machine.md
v0.12 §3.2「先寫属性、後 move」）。SS 側はこの property を読取り、控制面の
Firestore `jobs/{job_id}` 文檔と照合してから記帳処理へ進む——本モジュールは
その受信/照合ロジックを担う。

契約権威値（v0.10 定名、跨倉庫契約値：控制面書込・SS 読取、同名 key）：
    POSTING_ID_PROPERTY_KEY = "sandevistan_posting_id"
    改名即破壊交棒——単体でも複数リポジトリでも勝手に変えない。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# 契約 v0.10 §6 跨倉庫契約値（控制面寫、SS 讀同名 key）——改名即破壊交棒
POSTING_ID_PROPERTY_KEY = "sandevistan_posting_id"


def resolve_posting_id(file: Mapping[str, Any]) -> str | None:
    """Drive file dict の公開 properties から base posting_id を読取る（純関数）。

    以下いずれも None（防御的仕様。Drive API は本来 str→str のみ返すが、
    fake/破損データでも安全に縮退させる）：
        - properties キー自体が無い、または None
        - properties はあるが POSTING_ID_PROPERTY_KEY が無い
        - 値が str でない
        - 値が空文字列／空白のみ

    正常値は前後の空白を strip して返す。
    """
    properties = file.get("properties") or {}
    value = properties.get(POSTING_ID_PROPERTY_KEY)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped
