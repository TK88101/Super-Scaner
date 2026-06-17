"""ocr_engine の PaddleOCR メモリ対策(降采样守卫 + mobile モデル既定)の単体テスト。

巨大スキャン(例: 6300x8400)で server モデルが ~30GB を消費し OOM(SIGKILL)
する実測を受けた対策の回帰を保証する。

実行: venv311/bin/python -m pytest test_ocr_engine_memory.py -v
"""
import os
import sys
import unittest
from unittest.mock import patch

import numpy as np

# ocr_engine は読込時に GEMINI_API_KEY が無いと ValueError を送出するため補完。
os.environ.setdefault("GEMINI_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(__file__))

import config
import ocr_engine


class DownscaleForOcrTest(unittest.TestCase):
    """_downscale_for_ocr: 巨大画像のみ縮小、通常票は無変更。"""

    def setUp(self):
        self._orig = config.OCR_MAX_SIDE

    def tearDown(self):
        config.OCR_MAX_SIDE = self._orig

    def test_large_image_downscaled_to_cap_preserving_aspect(self):
        # Arrange: 6300x8400 の巨大スキャン相当 (h, w, c)
        config.OCR_MAX_SIDE = 3000
        arr = np.zeros((8400, 6300, 3), dtype=np.uint8)
        # Act
        out = ocr_engine._downscale_for_ocr(arr)
        # Assert: 最長辺が cap、アスペクト比保持
        h, w = out.shape[:2]
        self.assertEqual(max(h, w), 3000)
        self.assertAlmostEqual(w / h, 6300 / 8400, places=2)

    def test_normal_receipt_unchanged(self):
        # Arrange: A4 を dpi150 で描画した通常票相当 (~1754px)
        config.OCR_MAX_SIDE = 3000
        arr = np.zeros((1754, 1240, 3), dtype=np.uint8)
        # Act / Assert: 上限未満は無変更(同形状)
        out = ocr_engine._downscale_for_ocr(arr)
        self.assertEqual(out.shape, arr.shape)

    def test_cap_zero_disables_downscale(self):
        # Arrange: cap=0 は機能無効
        config.OCR_MAX_SIDE = 0
        arr = np.zeros((8400, 6300, 3), dtype=np.uint8)
        # Act / Assert
        out = ocr_engine._downscale_for_ocr(arr)
        self.assertEqual(out.shape, arr.shape)


class GetPaddleOcrModelTierTest(unittest.TestCase):
    """_get_paddle_ocr: OCR_MODEL_TIER に応じ mobile/server モデルを選択。"""

    def setUp(self):
        self._orig_tier = config.OCR_MODEL_TIER
        ocr_engine._paddle_ocr = None  # シングルトンをリセット

    def tearDown(self):
        config.OCR_MODEL_TIER = self._orig_tier
        ocr_engine._paddle_ocr = None

    def test_mobile_tier_passes_mobile_model_names(self):
        # Arrange
        config.OCR_MODEL_TIER = "mobile"
        # Act: PaddleOCR 構築を mock し引数のみ検証
        with patch.object(ocr_engine, "PaddleOCR") as m:
            ocr_engine._get_paddle_ocr()
        # Assert: mobile モデル名を渡す
        kwargs = m.call_args.kwargs
        self.assertEqual(
            kwargs.get("text_detection_model_name"), "PP-OCRv5_mobile_det")
        self.assertEqual(
            kwargs.get("text_recognition_model_name"), "PP-OCRv5_mobile_rec")

    def test_server_tier_omits_model_names(self):
        # Arrange
        config.OCR_MODEL_TIER = "server"
        # Act
        with patch.object(ocr_engine, "PaddleOCR") as m:
            ocr_engine._get_paddle_ocr()
        # Assert: モデル名指定なし(= server 既定)
        kwargs = m.call_args.kwargs
        self.assertNotIn("text_detection_model_name", kwargs)
        self.assertNotIn("text_recognition_model_name", kwargs)


if __name__ == "__main__":
    unittest.main()
