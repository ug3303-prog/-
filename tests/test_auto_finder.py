# -*- coding: utf-8 -*-
"""
auto_finder 단위 테스트 (네트워크·KIS 키 없이 실행 가능).

실행:  python -m unittest discover -s tests -v   (저장소 루트에서)
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto_finder as af  # noqa: E402


class IsCommonStockTest(unittest.TestCase):
    def test_common_stocks_pass(self):
        # 예전 '이름 포함' 필터에서 잘못 빠지던 보통주들 (2026-11-03 KRX 데이터 기준 실제 종목)
        for code, name in [
            ("005930", "삼성전자"),
            ("138040", "메리츠금융지주"),       # '리츠' 포함 — 예전엔 빠졌음
            ("037270", "YG PLUS"),              # 'PLUS' 포함 — 예전엔 빠졌음
            ("458650", "성우"),                 # '우'로 끝남 — 예전엔 빠졌음
            ("159910", "에코글로우"),
            ("369370", "블리츠웨이엔터테인먼트"),
            ("0126Z0", "삼성에피스홀딩스"),     # 영문 섞인 신규 코드 체계
        ]:
            with self.subTest(name=name):
                self.assertTrue(af._is_common_stock(code, name))

    def test_preferred_excluded(self):
        for code, name in [
            ("005935", "삼성전자우"),
            ("005387", "현대차2우B"),
            ("00104K", "CJ4우(전환)"),          # 예전엔 통과했음
            ("00279K", "아모레퍼시픽홀딩스3우C"),  # 예전엔 통과했음
            ("37550L", "DL이앤씨2우(전환)"),    # 예전엔 통과했음
        ]:
            with self.subTest(name=name):
                self.assertFalse(af._is_common_stock(code, name))

    def test_spac_excluded(self):
        self.assertFalse(af._is_common_stock("474660", "신한제12호스팩"))
        # 이름에 '스팩'이 없는 스팩 — 예전엔 통과했음
        self.assertFalse(af._is_common_stock("0205H0", "에스케이증권제14호기업인수목적", "SPAC(소속부없음)"))
        self.assertFalse(af._is_common_stock("999990", "아무개", "SPAC(소속부없음)"))

    def test_reit_and_etf_excluded(self):
        self.assertFalse(af._is_common_stock("395400", "SK리츠"))
        self.assertFalse(af._is_common_stock("069500", "KODEX 200"))
        self.assertFalse(af._is_common_stock("102110", "TIGER 200"))
        self.assertFalse(af._is_common_stock("105190", "ACE 200"))


class LoadTopMarcapTest(unittest.TestCase):
    """FinanceDataReader를 가짜 모듈로 바꿔 load_top_marcap 전체 흐름을 확인."""

    def setUp(self):
        import pandas as pd
        self.df = pd.DataFrame({
            "Code": ["005930", "005935", "138040", "395400", "474660", "000001"],
            "Name": ["삼성전자", "삼성전자우", "메리츠금융지주", "SK리츠", "신한제12호스팩", "작은회사"],
            "Dept": ["", "", "", "", "관리종목(소속부없음)", ""],
            "Marcap": [x * 1e8 for x in (12_832_580, 1_376_868, 218_220, 14_539, 115, 500)],  # 억원 → 원
        })
        fake = types.ModuleType("FinanceDataReader")
        fake.StockListing = lambda _name: self.df.copy()
        self._orig = sys.modules.get("FinanceDataReader")
        sys.modules["FinanceDataReader"] = fake

    def tearDown(self):
        if self._orig is None:
            sys.modules.pop("FinanceDataReader", None)
        else:
            sys.modules["FinanceDataReader"] = self._orig

    def test_filters_and_order(self):
        out = af.load_top_marcap(top_n=10, min_mcap_억=1_000)
        self.assertEqual([c for c, _, _ in out], ["005930", "138040"])
        self.assertEqual(out[0][2], 12_832_580)  # 억 단위 변환

    def test_works_without_dept_column(self):
        self.df = self.df.drop(columns=["Dept"])
        out = af.load_top_marcap(top_n=10, min_mcap_억=1_000)
        self.assertEqual([c for c, _, _ in out], ["005930", "138040"])

    def test_top_n_limit(self):
        out = af.load_top_marcap(top_n=1, min_mcap_억=0)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
