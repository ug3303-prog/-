import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import auto_finder as af
import dart_feed as dart
import kis_feed as kf


def record(code, name, market_cap, **overrides):
    item = {
        "code": code,
        "name": name,
        "market": "KOSPI",
        "그룹코드": "ST",
        "SPAC": "N",
        "거래정지": "N",
        "정리매매": "N",
        "관리종목": "N",
        "시장경고": "00",
        "우선주": "0",
        "시가총액": str(market_cap),
    }
    item.update(overrides)
    return item


class MarcapUniverseTests(unittest.TestCase):
    def test_filters_risky_types_and_sorts_market_cap(self):
        records = [
            record("000001", "정상중형", 30_000),
            record("000002", "정상대형", 90_000),
            record("000003", "거래정지", 120_000, 거래정지="Y"),
            record("000004", "시장경고", 110_000, 시장경고="02"),
            record("000005", "스팩", 100_000, SPAC="Y"),
            record("000006", "우선주", 95_000, 우선주="1"),
            record("000007", "기준미달", 19_999),
        ]
        with patch.object(af, "_load_master_records", return_value=records):
            result = af.load_top_marcap(10, 20_000)
        self.assertEqual(
            [("000002", "정상대형", 90_000), ("000001", "정상중형", 30_000)],
            result,
        )

    def test_accepts_new_alphanumeric_stock_codes(self):
        with patch.object(
            af,
            "_load_master_records",
            return_value=[record("0015G0", "신규종목", 40_000)],
        ):
            result = af.load_top_marcap(1, 20_000)
        self.assertEqual("0015G0", result[0][0])

    def test_uses_stale_cache_when_download_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "master.json"
            payload = {"as_of": "2026-09-30", "records": [record("000001", "캐시종목", 30_000)]}
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(af, "_MASTER_CACHE", cache_path), patch.object(
                af, "_download_master_records", side_effect=requests.ConnectionError("offline")
            ):
                messages = []
                result = af._load_master_records(messages.append)
        self.assertEqual("캐시종목", result[0]["name"])
        self.assertTrue(any("저장된" in message for message in messages))




# ───────────────────────── 지표 계산 ─────────────────────────
def make_bars(n, close=100, volume=1000, today=None, start=20260101):
    """최신순 일봉 n개. date 는 큰 값이 최신."""
    bars = []
    for i in range(n):
        bars.append({"date": str(start + (n - 1 - i)), "high": close + 2, "low": close - 2,
                     "close": close, "volume": volume, "value": float(close * volume)})
    return bars


class ComputeMetricsTests(unittest.TestCase):
    def test_intraday_partial_bar_excluded_from_volume_stats(self):
        bars = make_bars(260)
        today = bars[0]["date"]
        bars[0]["volume"] = 10            # 장중이라 거래량이 아직 작음
        m = af.compute_metrics(bars, 20, 200, intraday=True, today=today)
        self.assertTrue(m["partial"])
        self.assertAlmostEqual(1.0, m["vol_ratio"])       # 직전 거래일 기준이라 평상시 1.0
        m2 = af.compute_metrics(bars, 20, 200, intraday=False, today=today)
        self.assertFalse(m2["partial"])
        self.assertLess(m2["vol_ratio"], 0.1)              # 장마감 후라면 그 봉도 쓴다

    def test_short_history_has_no_ma(self):
        m = af.compute_metrics(make_bars(120), 20, 200)
        self.assertFalse(m["ma_ok"])
        self.assertIsNone(m["ma_val"])

    def test_ma_slope_positive_when_prices_rising(self):
        bars = make_bars(260)
        for i, b in enumerate(bars):          # 최신일수록 비싸게
            b["close"] = 100 + (260 - i) * 0.1
        m = af.compute_metrics(bars, 20, 200)
        self.assertTrue(m["ma_ok"])
        self.assertGreater(m["ma_slope"], 0)

    def test_none_when_not_enough_rows(self):
        self.assertIsNone(af.compute_metrics(make_bars(10), 20, 200))


class FinancialTests(unittest.TestCase):
    def test_bank_by_sector_and_by_debt_ratio(self):
        self.assertTrue(af.is_financial("금융"))
        self.assertTrue(af.is_financial("", 1282.0, "KB금융"))
        self.assertFalse(af.is_financial("전기·전자", 90.0, "삼성전자"))


# ───────────────────────── 추천 3종목 ─────────────────────────
def cand(code, name, sector, **kw):
    base = dict(name=name, code=code, mcap=50_000, price=10_000, pullback=12.0, above_ma=15.0,
                trade_value=200.0, vol_ratio=1.0, ma_slope=2.0, sector=sector, per=10.0, pbr=1.0)
    base.update(kw)
    return af.Candidate(**base)


class PickTests(unittest.TestCase):
    def test_hard_filters(self):
        self.assertFalse(af.pick_eligibility(cand("1", "적자", "A", per=None, per_loss=True))[0])
        self.assertFalse(af.pick_eligibility(cand("2", "악재의심", "A", pullback=25.0))[0])
        self.assertFalse(af.pick_eligibility(cand("3", "덜눌림", "A", pullback=5.5))[0])
        self.assertFalse(af.pick_eligibility(cand("4", "하락MA", "A", ma_slope=-0.5))[0])
        self.assertFalse(af.pick_eligibility(cand("5", "거래량급증", "A", vol_ratio=4.0))[0])
        self.assertFalse(af.pick_eligibility(cand("6", "영업적자", "A", op_positive=False))[0])
        self.assertFalse(af.pick_eligibility(
            cand("7", "수급이탈", "A", flow_foreign=-10.0, flow_orgn=-5.0, flow_unit="억"))[0])
        self.assertTrue(af.pick_eligibility(cand("8", "정상", "A"))[0])

    def test_returns_three_with_distinct_sectors(self):
        cands = [
            cand("1", "A1", "반도체", pullback=12, per_rel=-20),
            cand("2", "A2", "반도체", pullback=12, per_rel=-25),
            cand("3", "B1", "자동차", pullback=11, per_rel=-10),
            cand("4", "C1", "화학", pullback=13, per_rel=0),
            cand("5", "D1", "은행", pullback=30),            # 탈락(눌림 과다)
        ]
        picks = af.pick_top(cands, n=3)
        self.assertEqual(3, len(picks))
        self.assertEqual(3, len({p.sector for p in picks}))
        self.assertNotIn("D1", [p.name for p in picks])
        self.assertTrue(all(p.score is not None and p.reasons for p in picks))

    def test_fills_from_same_sector_when_not_enough_sectors(self):
        cands = [cand(str(i), f"S{i}", "반도체") for i in range(4)]
        self.assertEqual(3, len(af.pick_top(cands, n=3)))

    def test_flow_function_can_remove_candidate(self):
        cands = [cand("1", "A", "x"), cand("2", "B", "y"), cand("3", "C", "z"), cand("4", "D", "w")]

        def flow(c):
            if c.code == "1":
                c.flow_foreign, c.flow_orgn, c.flow_unit = -10.0, -10.0, "억"
            else:
                c.flow_foreign, c.flow_orgn, c.flow_unit = 5.0, 5.0, "억"

        picks = af.pick_top(cands, n=3, flow_fn=flow)
        self.assertEqual({"B", "C", "D"}, {p.name for p in picks})


# ───────────────────────── DART ─────────────────────────
class DartMetricsTests(unittest.TestCase):
    def rows(self):
        def row(name, th, fr, tha=None, fra=None):
            return {"fs_div": "CFS", "account_nm": name, "thstrm_amount": th, "frmtrm_amount": fr,
                    "thstrm_add_amount": tha, "frmtrm_add_amount": fra}
        return [row("부채총계", "200", "150"), row("자본총계", "100", "100"),
                row("영업수익", "50", "40", "110", "80"), row("영업이익", "10", "8", "30", "20"),
                row("당기순이익", "7", "5", "20", "12")]

    def test_uses_cumulative_for_half_year_and_new_revenue_alias(self):
        acc = dart._extract_accounts(self.rows())
        m = dart._compute_metrics(acc, 2026, "11012")
        self.assertAlmostEqual(200.0, m["debt_ratio"])
        self.assertAlmostEqual(50.0, m["op_profit_yoy"])      # (30-20)/20
        self.assertAlmostEqual(37.5, m["revenue_yoy"])        # (110-80)/80, '영업수익' 별칭
        self.assertIn("누적", m["period"])

    def test_turnaround_and_no_negative_base(self):
        acc = {"operating_profit": {"thstrm": 5.0, "frmtrm": -10.0, "thstrm_add": None, "frmtrm_add": None}}
        m = dart._compute_metrics(acc, 2026, "11011")
        self.assertIsNone(m["op_profit_yoy"])
        self.assertTrue(m["op_turnaround"])


class DartCacheTests(unittest.TestCase):
    def run_fetch(self, get_side_effect):
        saved = []
        with patch.object(dart, "get_corp_code", return_value="00000001"), \
             patch.object(dart, "_load_fin_cache", return_value={}), \
             patch.object(dart, "_save_fin_cache", side_effect=lambda c: saved.append(c)), \
             patch.object(dart, "_get", side_effect=get_side_effect):
            try:
                result = dart.fetch_financials("005930")
                err = None
            except Exception as e:
                result, err = None, e
        return result, err, saved

    def test_network_error_is_not_cached(self):
        result, err, saved = self.run_fetch(requests.ConnectionError("down"))
        self.assertIsNone(result)
        self.assertIsNone(err)
        self.assertEqual([], saved)

    def test_no_data_is_cached_short(self):
        class R:
            def json(self):
                return {"status": "013", "message": "no data"}
        result, err, saved = self.run_fetch(lambda *a, **k: R())
        self.assertIsNone(result)
        self.assertEqual(1, len(saved))
        self.assertIsNone(saved[0]["00000001"]["metrics"])

    def test_bad_key_raises_fatal(self):
        class R:
            def json(self):
                return {"status": "010", "message": "unregistered key"}
        result, err, saved = self.run_fetch(lambda *a, **k: R())
        self.assertIsInstance(err, dart.DartFatalError)
        self.assertEqual([], saved)


# ───────────────────────── KIS ─────────────────────────
class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class KisTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(kf._state)

    def tearDown(self):
        kf._state.clear()
        kf._state.update(self._saved)

    def test_gui_keys_are_not_overwritten_by_key_file(self):
        kf.set_keys("GUIKEY", "GUISECRET", True)
        kf._state["access_token"], kf._state["token_expire"] = "tok", 9e12
        with patch.object(kf, "_read_key_file", return_value={"app_key": "FILE", "app_secret": "S", "is_virtual": False}):
            kf._load_keys()
        self.assertEqual("GUIKEY", kf._state["app_key"])
        self.assertTrue(kf._state["is_virtual"])

    def test_jwt_exp_parsed(self):
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1791852657}).encode()).decode().rstrip("=")
        self.assertEqual(1791852657.0, kf._jwt_exp(f"h.{payload}.s"))
        self.assertIsNone(kf._jwt_exp("garbage"))

    def test_expired_token_is_reissued_and_retried(self):
        kf.set_keys("K", "S", False)
        kf._state["access_token"], kf._state["token_expire"] = "old", 9e12
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(headers["authorization"])
            if len(calls) == 1:
                return FakeResp(500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token"})
            return FakeResp(200, {"rt_cd": "0", "output": {"ok": 1}})

        def fake_issue():
            kf._state["access_token"], kf._state["token_expire"] = "new", 9e12
            return "new"

        with patch.object(kf.requests, "get", side_effect=fake_get), \
             patch.object(kf, "_issue_token", side_effect=fake_issue), \
             patch.object(kf, "_invalidate_token", side_effect=lambda: kf._state.update(access_token=None, token_expire=0.0)), \
             patch.object(kf, "_throttle"):
            body = kf._get("/x", "T", {})
        self.assertEqual({"ok": 1}, body["output"])
        self.assertEqual(["Bearer old", "Bearer new"], calls)

    def test_realtime_flags_loss_and_risk(self):
        out = {"stck_prpr": "1,000", "per": "0.00", "pbr": "1.2", "eps": "-50", "bstp_kor_isnm": "제약",
               "temp_stop_yn": "N", "mrkt_warn_cls_code": "02", "mang_issu_cls_code": "N", "sltr_yn": "N"}
        with patch.object(kf, "_get", return_value={"output": out}):
            rt = kf.fetch_realtime("000001")
        self.assertIsNone(rt["per"])
        self.assertTrue(rt["per_loss"])
        self.assertEqual(["시장경고"], rt["risk"])
        self.assertEqual("제약", rt["sector"])


class ReitClassificationTests(unittest.TestCase):
    """'리츠' 포함 검사로 메리츠금융지주가 빠지던 버그 회귀 테스트 (2026-11-03 KRX 실데이터 기준 종목)."""

    def _records(self):
        import pandas as pd
        df = pd.DataFrame({
            "Code": ["138040", "369370", "395400", "0030R0", "123450"],
            "Name": ["메리츠금융지주", "블리츠웨이엔터테인먼트", "SK리츠", "대신밸류리츠", "메리츠제1호스팩"],
            "Dept": ["", "벤처기업부", "", "", "SPAC(소속부없음)"],
            "Marcap": [218_220e8, 256e8, 14_539e8, 2_137e8, 100e8],
        })
        return {r["name"]: r for r in af._df_to_records(df)}

    def test_meritz_and_blitzway_are_common_stock(self):
        recs = self._records()
        self.assertEqual("ST", recs["메리츠금융지주"]["그룹코드"])
        self.assertEqual("ST", recs["블리츠웨이엔터테인먼트"]["그룹코드"])

    def test_reits_still_excluded(self):
        recs = self._records()
        self.assertEqual("RT", recs["SK리츠"]["그룹코드"])
        self.assertEqual("RT", recs["대신밸류리츠"]["그룹코드"])

    def test_meritz_spac_is_spac_not_reit(self):
        rec = self._records()["메리츠제1호스팩"]
        self.assertEqual("ST", rec["그룹코드"])
        self.assertEqual("Y", rec["SPAC"])

    def test_meritz_enters_universe(self):
        recs = list(self._records().values())
        with patch.object(af, "_load_master_records", return_value=recs):
            out = af.load_top_marcap(10, 1_000)
        self.assertEqual(["138040"], [c for c, _, _ in out])


class ScalperFeedTests(unittest.TestCase):
    """scalper.py가 쓰는 현재가 확장 필드·호가 조회 (필드명은 mock 기준 — 실응답 미검증)."""

    def test_realtime_scalper_fields(self):
        out = {"stck_prpr": "70,000", "per": "12.5", "pbr": "1.1", "prdy_vrss": "1500", "prdy_vrss_sign": "5",
               "prdy_ctrt": "-2.10", "acml_vol": "1,234,567", "shnu_cntg_qty": "300", "seln_cntg_qty": "200"}
        with patch.object(kf, "_get", return_value={"output": out}):
            rt = kf.fetch_realtime("005930")
        self.assertEqual(-1500, rt["change"])
        self.assertEqual(-2.1, rt["change_pct"])
        self.assertEqual(1_234_567, rt["volume"])
        self.assertEqual(150.0, rt["cttr"])
        self.assertEqual(12.5, rt["per"])   # 기존 필드는 그대로

    def test_realtime_cttr_none_when_missing(self):
        with patch.object(kf, "_get", return_value={"output": {"stck_prpr": "100"}}):
            rt = kf.fetch_realtime("005930")
        self.assertIsNone(rt["cttr"])
        self.assertIsNone(rt["change"])

    def test_orderbook(self):
        out = {"askp1": "70,100", "bidp1": "70,000", "askp_rsqn1": "10", "bidp_rsqn1": "20",
               "total_askp_rsqn": "1000", "total_bidp_rsqn": "1800"}
        with patch.object(kf, "_get", return_value={"output1": out}):
            ob = kf.fetch_orderbook("005930")
        self.assertEqual(70_100, ob["ask1"])
        self.assertEqual(1.8, ob["bid_ask_ratio"])
        self.assertEqual((70_100, 10, 70_000, 20), ob["levels"][0])
        self.assertEqual(10, len(ob["levels"]))

    def test_orderbook_empty(self):
        with patch.object(kf, "_get", return_value={"output1": {}}):
            self.assertIsNone(kf.fetch_orderbook("005930"))


class FormatTests(unittest.TestCase):
    def test_yoy_format(self):
        self.assertEqual("N/M", af.fmt_yoy(8118.0))
        self.assertEqual("+12.3", af.fmt_yoy(12.34))
        self.assertEqual("흑전", af.fmt_yoy(None, turnaround=True))
        self.assertEqual("-", af.fmt_yoy(None))


if __name__ == "__main__":
    unittest.main()
