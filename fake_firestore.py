"""テスト用の軽量 fake Firestore（IP-301/304 の台賬テストで共用）。

path タプルを鍵にした dict ストアで、`jobs/{k}/postings/{p}` のような入れ子
サブコレクションと `transaction.set()` を最小限に再現する。gspread/main へ依存
しない（＝台賬単体テストは重い import なしで走る）。txn は `body(fake_txn)` で駆動。
"""

from __future__ import annotations


class _Snap:
    def __init__(self, data):
        self._data = None if data is None else dict(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, fs, path):
        self._fs = fs
        self._path = path

    def collection(self, name):
        return _CollRef(self._fs, self._path + (name,))

    def get(self, transaction=None):
        return _Snap(self._fs.store.get(self._path))


class _CollRef:
    def __init__(self, fs, path):
        self._fs = fs
        self._path = path

    def document(self, doc_id):
        return _DocRef(self._fs, self._path + (doc_id,))


class _Txn:
    def __init__(self, fs):
        self._fs = fs

    def set(self, doc_ref, data):
        self._fs.store[doc_ref._path] = dict(data)


class FakeFirestore:
    """メモリ Firestore（跨 run で store を保持＝崩潰後の台賬状態を再現）。"""

    def __init__(self):
        self.store: dict = {}

    def collection(self, name):
        return _CollRef(self, (name,))

    def runner(self):
        """transaction_runner 注入用：body(fake_txn) をそのまま実行する。"""
        return lambda body: body(_Txn(self))
