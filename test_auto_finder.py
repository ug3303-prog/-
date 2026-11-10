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

    def test_just_crossed_ma_is_rejected(self):
        """2026-11-10 실사용 회귀: above_ma 3.9%(KT&G)/4.7%(DB손해보험)짜리가 추천됐다가
        손실 — '막 돌파'는 더 이상 통과하면 안 된다(PICK_MIN_ABOVE_MA 6.0% 상향)."""
        self.assertFalse(af.pick_eligibility(cand("1", "KT&G 재현", "A", above_ma=3.9))[0])
        self.assertFalse(af.pick_eligibility(cand("2", "DB손해보험 재현", "A", above_ma=4.7))[0])
        self.assertTrue(af.pick_eligibility(cand("3", "충분히위", "A", above_ma=10.0))[0])

    def test_one_day_breakout_is_rejected_even_if_above_ma_is_large_enough(self):
        """above_ma 수치만으론 '막 돌파'를 못 잡는 경우를 ma_confirm_ratio로 보완한다 —
        오늘만 살짝 위, 최근 5일 중 하루만 200일선 위였다면 탈락해야 한다."""
        c = cand("1", "하루만위", "A", above_ma=10.0, ma_confirm_ratio=0.2)
        self.assertFalse(af.pick_eligibility(c)[0])
        c2 = cand("2", "꾸준히위", "A", above_ma=10.0, ma_confirm_ratio=0.8)
        self.assertTrue(af.pick_eligibility(c2)[0])

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


# ───────────────────────── 단타 반등 후보(칼날주의) ─────────────────────────
def knife_cand(code, name, sector, **kw):
    """칼날주의(200일선 아래) 기본값의 Candidate. above_ma는 음수여야 한다."""
    base = dict(name=name, code=code, mcap=50_000, price=10_000, pullback=20.0, above_ma=-10.0,
                trade_value=200.0, vol_ratio=2.0, ma_slope=-1.0, sector=sector, per=10.0, pbr=1.0)
    base.update(kw)
    return af.Candidate(**base)


class BounceTests(unittest.TestCase):
    def test_hard_filters(self):
        self.assertFalse(af.bounce_eligibility(knife_cand("1", "적자", "A", per=None, per_loss=True))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("2", "덜빠짐", "A", pullback=10.0))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("3", "구조붕괴", "A", pullback=50.0))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("4", "MA근접", "A", above_ma=-1.0))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("5", "MA과다이탈", "A", above_ma=-40.0))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("6", "패닉거래량", "A", vol_ratio=5.0))[0])
        self.assertFalse(af.bounce_eligibility(knife_cand("7", "영업적자", "A", op_positive=False))[0])
        self.assertFalse(af.bounce_eligibility(
            knife_cand("8", "수급이탈", "A", flow_foreign=-10.0, flow_orgn=-5.0, flow_unit="억"))[0])
        self.assertTrue(af.bounce_eligibility(knife_cand("9", "정상", "A"))[0])

    def test_returns_three_with_distinct_sectors(self):
        cands = [
            knife_cand("1", "A1", "반도체", pullback=20, per_rel=-20),
            knife_cand("2", "A2", "반도체", pullback=22, per_rel=-25),
            knife_cand("3", "B1", "자동차", pullback=18, per_rel=-10),
            knife_cand("4", "C1", "화학", pullback=25, per_rel=0),
            knife_cand("5", "D1", "은행", pullback=5),            # 탈락(낙폭 부족)
        ]
        picks = af.bounce_pick_top(cands, n=3)
        self.assertEqual(3, len(picks))
        self.assertEqual(3, len({p.sector for p in picks}))
        self.assertNotIn("D1", [p.name for p in picks])
        self.assertTrue(all(p.score is not None and p.reasons for p in picks))

    def test_higher_volume_ratio_scores_better_than_swing(self):
        """스윙(score_candidate)은 거래량 과열을 감점하지만, 반등후보는 어느 정도까지는
        가점으로 뒤집는다는 설계 의도를 확인한다."""
        low_vol = knife_cand("1", "저거래량", "A", vol_ratio=0.3)
        mid_vol = knife_cand("2", "중거래량", "A", vol_ratio=2.0)
        s_low, _ = af.score_bounce_candidate(low_vol)
        s_mid, _ = af.score_bounce_candidate(mid_vol)
        self.assertGreater(s_mid, s_low)


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
               "prdy_ctrt": "-2.10", "acml_vol": "1,234,567"}
        with patch.object(kf, "_get", return_value={"output": out}):
            rt = kf.fetch_realtime("005930")
        self.assertEqual(-1500, rt["change"])
        self.assertEqual(-2.1, rt["change_pct"])
        self.assertEqual(1_234_567, rt["volume"])
        self.assertEqual(12.5, rt["per"])   # 기존 필드는 그대로

    def test_cttr_from_inquire_ccnl(self):
        # 주식현재가 체결 API: output은 최근 체결순 배열, tday_rltv = 당일 체결강도
        body = {"output": [{"stck_cntg_hour": "101500", "tday_rltv": "123.45"},
                           {"stck_cntg_hour": "101459", "tday_rltv": "120.00"}]}
        with patch.object(kf, "_get", return_value=body) as g:
            self.assertEqual(123.45, kf.fetch_cttr("005930"))
        self.assertEqual("FHKST01010300", g.call_args.kwargs["tr_id"])

    def test_cttr_none_when_missing(self):
        with patch.object(kf, "_get", return_value={"output": []}):
            self.assertIsNone(kf.fetch_cttr("005930"))
        with patch.object(kf, "_get", return_value={"output": [{"tday_rltv": ""}]}):
            self.assertIsNone(kf.fetch_cttr("005930"))

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


def _import_scalper():
    """tkinter가 없는 환경(CI 등)에서도 Watcher 로직만 테스트할 수 있게 더미 모듈을 끼운다."""
    import sys
    import types
    try:
        import tkinter  # noqa: F401
    except ImportError:
        for m in ("tkinter", "tkinter.ttk", "tkinter.messagebox"):
            sys.modules.setdefault(m, types.ModuleType(m))
        sys.modules["tkinter"].Tk = object
    import scalper
    return scalper


class ScalperSignalTests(unittest.TestCase):
    def setUp(self):
        self.sc = _import_scalper()
        self.w = self.sc.Watcher("021240", "코웨이")

    def step(self, price, volume, cttr=100, ratio=1.0):
        sig = self.w.update({"price": price, "volume": volume, "cttr": cttr}, {"bid_ask_ratio": ratio})
        return sig, self.w.signals_to_log(sig)

    def test_tiny_volume_jump_is_not_surge(self):
        # 실사용에서 본 '거래량 급증(94주)' — 배수 조건은 넘지만 거래대금 1억 미만이면 무시
        self.step(102_000, 1_000)
        self.step(102_000, 1_020)            # +20주
        sig, _ = self.step(102_000, 1_114)   # +94주 ≈ 959만원
        self.assertEqual([], sig)

    def test_large_volume_jump_is_surge(self):
        self.step(102_000, 1_000)
        self.step(102_000, 1_020)
        sig, logged = self.step(102_000, 3_020)   # +2,000주 ≈ 2.04억
        self.assertTrue(any(x.startswith("거래량 급증") for x in sig))
        self.assertEqual(sig, logged)             # 이벤트형은 매번 로그

    def test_state_signal_logged_only_on_change(self):
        _, logged = self.step(102_000, 1_000, ratio=2.8)
        self.assertEqual(["호가 매수우위(2.8배)"], logged)
        sig, logged = self.step(102_000, 1_000, ratio=2.9)
        self.assertEqual(["호가 매수우위(2.9배)"], sig)   # 표에는 계속 표시
        self.assertEqual([], logged)                       # 로그엔 반복 안 함
        _, logged = self.step(102_000, 1_000, ratio=0.5)
        self.assertEqual(["호가 매도우위(2.0배)"], logged)  # 방향이 바뀌면 다시 로그
        self.step(102_000, 1_000, ratio=1.0)                # 해제
        _, logged = self.step(102_000, 1_000, ratio=2.0)
        self.assertEqual(["호가 매수우위(2.0배)"], logged)  # 해제 후 재발생도 로그


class FormatTests(unittest.TestCase):
    def test_yoy_format(self):
        self.assertEqual("N/M", af.fmt_yoy(8118.0))
        self.assertEqual("+12.3", af.fmt_yoy(12.34))
        self.assertEqual("흑전", af.fmt_yoy(None, turnaround=True))
        self.assertEqual("-", af.fmt_yoy(None))


if __name__ == "__main__":
    unittest.main()
