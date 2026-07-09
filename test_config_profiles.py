"""config.py の出力プロファイル解決の単体テスト。

主目的: 社長専用の受限フォルダ配下へ書き込む独立通道(ido / rental)を
追加するにあたり、以下 2 点を保証する。

1. 本番 PC の .env 更新漏れでボット全体が止まらないこと。
   新プロファイルの env が欠けていても、共通(default)は必ず動き続ける。
2. フォルダ ID から書き込み先が一意に決まること。
   誤ったプロファイルへ書くと、社長の仕訳が全社共有シートへ漏れる。

config.py は os と doc_types しか import しないため、venv311 不要:
    /usr/bin/python3 -m unittest test_config_profiles -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import config
from doc_types import DocType

# 合成 ID（本番値は使わない）
ENV_FULL = {
    # --- 共通(default)
    "OUTPUT_SPREADSHEET_ID": "ss_default",
    "PROCESSED_FOLDER_ID": "fid_default_processed",
    "SPLIT_PDF_FOLDER_ID": "fid_default_split",
    "FOLDER_RECEIPT_ID": "fid_default_receipt",
    "FOLDER_PURCHASE_INVOICE_ID": "fid_default_purchase",
    # --- 井戸会計事務所
    "IDO_OUTPUT_SPREADSHEET_ID": "ss_ido",
    "IDO_PROCESSED_FOLDER_ID": "fid_ido_processed",
    "IDO_SPLIT_PDF_FOLDER_ID": "fid_ido_split",
    "IDO_FOLDER_RECEIPT_ID": "fid_ido_receipt",
    "IDO_FOLDER_SALES_INVOICE_ID": "fid_ido_sales",
    # --- レンタル経理部
    "RENTAL_OUTPUT_SPREADSHEET_ID": "ss_rental",
    "RENTAL_PROCESSED_FOLDER_ID": "fid_rental_processed",
    "RENTAL_SPLIT_PDF_FOLDER_ID": "fid_rental_split",
    "RENTAL_FOLDER_RECEIPT_ID": "fid_rental_receipt",
    "RENTAL_FOLDER_SALARY_SLIP_ID": "fid_rental_salary",
}


def env(**overrides):
    """ENV_FULL をベースに、値 None のキーを削除した環境を作る。"""
    merged = dict(ENV_FULL)
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return patch.dict(os.environ, merged, clear=True)


class LoadProfilesTest(unittest.TestCase):

    def test_returns_all_three_profiles_when_fully_configured(self):
        with env():
            profiles = config.load_profiles()

        self.assertEqual({"default", "ido", "rental"}, set(profiles))
        self.assertEqual("ss_ido", profiles["ido"]["spreadsheet_id"])
        self.assertEqual("fid_ido_processed", profiles["ido"]["processed_folder_id"])
        self.assertEqual("fid_ido_split", profiles["ido"]["split_pdf_folder_id"])

    def test_each_profile_carries_a_human_readable_label(self):
        with env():
            profiles = config.load_profiles()

        self.assertEqual("井戸会計事務所", profiles["ido"]["label"])
        self.assertEqual("株式会社レンタル経理部", profiles["rental"]["label"])

    def test_profile_is_skipped_when_its_spreadsheet_id_is_missing(self):
        """本番 .env の更新漏れで ido が欠けても default は生き残る。"""
        with env(IDO_OUTPUT_SPREADSHEET_ID=None):
            profiles = config.load_profiles()

        self.assertNotIn("ido", profiles)
        self.assertIn("default", profiles)
        self.assertIn("rental", profiles)

    def test_profile_is_skipped_when_its_processed_folder_is_missing(self):
        """アーカイブ先が無いプロファイルは有効化しない（処理後に迷子になる）。"""
        with env(RENTAL_PROCESSED_FOLDER_ID=None):
            profiles = config.load_profiles()

        self.assertNotIn("rental", profiles)

    def test_split_pdf_folder_is_optional(self):
        """未設定なら空文字。PageUrlResolver が base_url#page=N に降格する。"""
        with env(IDO_SPLIT_PDF_FOLDER_ID=None):
            profiles = config.load_profiles()

        self.assertIn("ido", profiles)
        self.assertEqual("", profiles["ido"]["split_pdf_folder_id"])

    def test_returns_empty_when_nothing_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual({}, config.load_profiles())


class LoadFolderMapTest(unittest.TestCase):

    def test_maps_each_folder_to_its_doc_type_and_profile(self):
        with env():
            folder_map = config.load_folder_map()

        self.assertEqual((DocType.RECEIPT, "default"), folder_map["fid_default_receipt"])
        self.assertEqual((DocType.PURCHASE_INVOICE, "default"), folder_map["fid_default_purchase"])
        self.assertEqual((DocType.RECEIPT, "ido"), folder_map["fid_ido_receipt"])
        self.assertEqual((DocType.SALES_INVOICE, "ido"), folder_map["fid_ido_sales"])
        self.assertEqual((DocType.RECEIPT, "rental"), folder_map["fid_rental_receipt"])
        self.assertEqual((DocType.SALARY_SLIP, "rental"), folder_map["fid_rental_salary"])

    def test_same_doc_type_across_profiles_does_not_collide(self):
        """3 プロファイルとも領収書フォルダを持つが、互いに上書きしない。"""
        with env():
            folder_map = config.load_folder_map()

        receipts = {fid: pk for fid, (dt, pk) in folder_map.items() if dt == DocType.RECEIPT}
        self.assertEqual(
            {"fid_default_receipt": "default",
             "fid_ido_receipt": "ido",
             "fid_rental_receipt": "rental"},
            receipts,
        )

    def test_disabled_profile_contributes_no_folders(self):
        """ido のシートが未設定なら、ido の入力フォルダは監視対象外。"""
        with env(IDO_OUTPUT_SPREADSHEET_ID=None):
            folder_map = config.load_folder_map()

        self.assertNotIn("fid_ido_receipt", folder_map)
        self.assertNotIn("fid_ido_sales", folder_map)
        self.assertIn("fid_default_receipt", folder_map)

    def test_accepts_precomputed_profiles(self):
        """main.py は load_profiles() を 1 回だけ呼び、その結果を渡す。"""
        with env():
            profiles = config.load_profiles()
            del profiles["rental"]
            folder_map = config.load_folder_map(profiles)

        self.assertNotIn("fid_rental_receipt", folder_map)
        self.assertIn("fid_ido_receipt", folder_map)


class DuplicateFolderIdTest(unittest.TestCase):
    """同一フォルダ ID が複数プロファイルに設定されたら即座に落とす。

    .env は手動編集で、IDO_ の塊をコピーして RENTAL_ に書き換える運用になる。
    1 行書き換え忘れると 2 プロファイルが同じ入力フォルダを指す。dict 代入は
    黙って後勝ちするため、社長専用の受限票据が全社共有シートへ流出しうる。
    起動時に落として気付かせる方が、静かに漏れ続けるより遥かに安全。
    """

    def test_raises_when_two_profiles_share_an_input_folder(self):
        with env(IDO_FOLDER_RECEIPT_ID="fid_default_receipt"):
            with self.assertRaises(ValueError) as ctx:
                config.load_folder_map()

        message = str(ctx.exception)
        self.assertIn("fid_default_receipt", message)
        self.assertIn("default", message)
        self.assertIn("ido", message)

    def test_raises_when_legacy_folder_collides_with_a_profile_folder(self):
        """INPUT_FOLDER_ID が他プロファイルの入力フォルダと同一のケース。"""
        with env(FOLDER_RECEIPT_ID=None, FOLDER_PURCHASE_INVOICE_ID=None,
                 INPUT_FOLDER_ID="fid_ido_receipt"):
            with self.assertRaises(ValueError) as ctx:
                config.load_folder_map()

        self.assertIn("fid_ido_receipt", str(ctx.exception))

    def test_same_folder_id_for_two_doc_types_in_one_profile_also_raises(self):
        """1 プロファイル内で領収書と請求書に同じフォルダを当てた場合。"""
        with env(IDO_FOLDER_SALES_INVOICE_ID="fid_ido_receipt"):
            with self.assertRaises(ValueError) as ctx:
                config.load_folder_map()

        self.assertIn("fid_ido_receipt", str(ctx.exception))

    def test_distinct_folders_do_not_raise(self):
        with env():
            folder_map = config.load_folder_map()

        # ENV_FULL は default 2 / ido 2 / rental 2 の計 6 フォルダ
        self.assertEqual(6, len(folder_map))


class LegacyInputFolderTest(unittest.TestCase):
    """旧来の単一フォルダ運用 (INPUT_FOLDER_ID) を壊さない。"""

    def test_legacy_folder_is_used_when_default_has_no_typed_folders(self):
        with env(FOLDER_RECEIPT_ID=None, FOLDER_PURCHASE_INVOICE_ID=None,
                 INPUT_FOLDER_ID="fid_legacy"):
            folder_map = config.load_folder_map()

        self.assertEqual((DocType.RECEIPT, "default"), folder_map["fid_legacy"])

    def test_legacy_folder_is_ignored_when_typed_folders_exist(self):
        with env(INPUT_FOLDER_ID="fid_legacy"):
            folder_map = config.load_folder_map()

        self.assertNotIn("fid_legacy", folder_map)

    def test_legacy_folder_does_not_leak_into_other_profiles(self):
        """default が無効なら、INPUT_FOLDER_ID があっても誰も拾わない。"""
        with env(OUTPUT_SPREADSHEET_ID=None, INPUT_FOLDER_ID="fid_legacy"):
            folder_map = config.load_folder_map()

        self.assertNotIn("fid_legacy", folder_map)


if __name__ == "__main__":
    unittest.main()
