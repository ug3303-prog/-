# -*- coding: utf-8 -*-
"""
대장주 단기스윙 스캐너 v1 — danta_v16 피더 재사용판
══════════════════════════════════════════════════════════════════
단타 스캐너(danta_v16)가 시총 1조 넘는 대형주를 걸러내기 때문에,
"대장주를 다른 로직으로 치는" 전용 도구가 필요해서 만든 프로그램.

[danta_v16과 뭐가 다른가 — 대형주는 물리가 다름]
  · 동력: 소형주는 거래량 급가속, 대형주는 "수급(외국인·기관·프로그램)"
  · 보유: 당일~1일(단타) → 2~10일(단기스윙)
  · 칼날 필터: 20·60일선 → 120·200일선 (장기추세)
  · 과열 차단: 당일 +15% → +8% (대형주는 하루 8%만 올라도 과열)
  · 핵심 신호: 정배열 속 눌림목 + 수급 전환 + 반등 확인

[danta_v16을 수정하지 않는다]
  이 파일은 danta_v16을 '피더(부품)'로 import만 한다. KIS 연결·토큰·DART·
  ATR 손절·수량계산은 그쪽의 검증된 함수를 그대로 쓰고, 유니버스와 채점만
  여기서 새로 한다. 그래서 단타 스캐너는 전혀 건드리지 않는다.

필요 파일: danta_v16.py, kis_config.json (app_key/app_secret/mode/dart_api_key)
           — 전부 이 파일과 같은 폴더에.

사용법:
  python swing_v1.py                      ← 1회 스캔 + 추천 포트폴리오
  python swing_v1.py --top 150            ← 시총 상위 150개 스캔
  python swing_v1.py --mcap-min 20000     ← 시총 2조 이상만
  python swing_v1.py --capital 9000000 --positions 3 --stop 5
  python swing_v1.py --no-excel           ← 엑셀 기록 끔
  python swing_v1.py --selftest           ← API 없이 채점 로직만 점검
"""
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment

import danta_v16 as D   # 피더 재사용 — 이 import가 핵심. danta_v16은 수정하지 않는다.

SCRIPT_DIR = Path(__file__).resolve().parent

# ══════════════════════════════════════════════════════════════════
# 스윙 파라미터 기본값
# ══════════════════════════════════════════════════════════════════
DEFAULTS = {
    "capital": 9_000_000,   # 투입 자금(원). 대장주 슬리브와 겹치면 본인이 조정
    "positions": 3,         # 동시 보유 목표 종목 수
    "stop": 5.0,            # 손절 상한(%) — 실제론 ATR이 더 좁으면 그걸 씀
    "risk": 1.0,            # 종목당 최대손실 = 자본의 1%
    "mcap_min": 10000,      # 시총 하한(억) — 기본 1조(= 대장주 정의)
    "mcap_max": 0,          # 시총 상한(억). 0 = 무제한
    "min_amount": 50,       # 20일 평균 거래대금 하한(억) — 대형주라 높게
    "top_n": 120,           # 시총 상위 몇 개를 스캔할지
}

# 등급 임계값 — 대형주는 소형주처럼 점수가 안 튀어서 단타보다 약간 낮게 잡음
GRADE_BASE = {"🚀선도매수": 55, "⬆매수대기": 42, "👀관심": 28}

# 대형주 과열/급락 차단 기준 (소형주보다 과열은 타이트, 급락은 명확히)
HOT_INTRADAY_BLOCK = 8.0     # 당일 +8%↑ → 추격 금지
HOT_LEAD_CAUTION   = 6.0     # 당일 +6%↑ → 선도매수 자격만 박탈(진입은 가능하나 과열권)
DROP_5D_BLOCK      = -15.0   # 5일 -15%↓ → 급락, 스윙 진입 부적합
DROP_FROMHI_BLOCK  = -25.0   # 20일 고점 대비 -25%↓ → 악재성 하락

MIN_BARS = 130               # 최소 확정 일봉(120일선 계산에 필요)
MA200_MIN = 200              # 200일선은 이만큼 있을 때만 사용, 없으면 120일선으로 대체

# ══════════════════════════════════════════════════════════════════
# 200일선용 깊은 일봉 조회 — v16 fetch_daily(~100봉)로는 200일선을 못 그린다
# ══════════════════════════════════════════════════════════════════
def fetch_daily_deep(code6: str, bars: int = 210):
    """KIS 일봉을 bars개까지 누적 (최신→과거).
    inquire-daily-itemchartprice는 1회 ~100봉 제한이라 날짜창을 과거로 밀며 모은다."""
    rows = {}
    end = datetime.now()
    for _ in range(4):                       # 최대 4창 (~약 280영업일 커버)
        start = end - timedelta(days=140)
        j = D._KIS.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6,
             "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        out = j.get("output2") or []
        got = 0
        for b in out:
            d = b.get("stck_bsop_date")
            if not d:
                continue
            c = D._i(b.get("stck_clpr"), 0)
            if c <= 0 or d in rows:
                continue
            rows[d] = {"date": d, "open": D._i(b.get("stck_oprc"), c), "high": D._i(b.get("stck_hgpr"), c),
                       "low": D._i(b.get("stck_lwpr"), c), "close": c,
                       "volume": D._i(b.get("acml_vol"), 0), "amount": D._i(b.get("acml_tr_pbmn"), 0)}
            got += 1
        if len(rows) >= bars or got == 0:
            break
        end = datetime.strptime(min(rows.keys()), "%Y%m%d") - timedelta(days=1)
    lst = sorted(rows.values(), key=lambda x: x["date"], reverse=True)
    return lst if len(lst) >= 6 else None


# ══════════════════════════════════════════════════════════════════
# 대장주 유니버스 — 시총 상위 N (danta_v16의 마스터 필터 재사용)
# ══════════════════════════════════════════════════════════════════
def build_universe_daejang(mcap_min, mcap_max, min_amount, top_n, log=print):
    try:
        master = D.load_master(log=log)
    except Exception as e:
        raise D.KISError(f"종목 마스터 다운로드 실패({e}) — 대장주 목록을 만들 수 없음")
    cands = D.master_filter(master, mcap_min, mcap_max, min_amount)   # 보통주·정상거래·시총·유동성 필터
    cands.sort(key=lambda c: -(c["mcap"] or 0))                       # 시총 상위순
    uni = cands[:top_n] if top_n > 0 else cands
    log(f"  시총 {mcap_min:,.0f}억 이상 · 상위 {len(uni)}개 선정 (전체 통과 {len(cands):,}개)\n")
    return uni


# ══════════════════════════════════════════════════════════════════
# 스윙 스코어 — 정배열·눌림목·수급·반등·상대강도 (합계 100)
# ══════════════════════════════════════════════════════════════════
def score_swing(daily, rt, inv, strength=None, filings=None, sector_avg=None):
    signals, warnings, blocks = [], [], []
    bd = {}
    today = datetime.now().strftime("%Y%m%d")
    mst = D.market_status()
    hist = [d for d in daily if str(d.get("date", "")) < today] if daily else []

    price = rt.get("price")
    prev_close = rt.get("prev_close") or (hist[0]["close"] if hist else None)
    base = {"total": 0, "bd": {}, "grade": "⛔차단", "signals": signals, "warnings": warnings,
            "blocks": blocks, "change_rate": rt.get("change_rate") or 0.0, "mcap": rt.get("_mcap"),
            "ma20": None, "ma60": None, "ma120": None, "ma200": None, "pullback": None,
            "vol_ratio": None, "vwap": rt.get("vwap"), "strength": strength,
            "fore_3d": (inv or {}).get("fore_3d"), "inst_3d": (inv or {}).get("inst_3d"),
            "prog_net": rt.get("_pgtr_ntby"), "rate_5d": None, "_bear_today": False}

    if not price or not prev_close or len(hist) < MIN_BARS:
        blocks.append(f"데이터 부족 (확정일봉 {len(hist)}개 < {MIN_BARS})")
        return base

    closes = [d["close"] for d in hist]
    vols = [d["volume"] for d in hist]
    highs = [d["high"] for d in hist]

    def ma(n, shift=0):
        seg = closes[shift:shift + n]
        return sum(seg) / n if len(seg) == n else None

    ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
    ma120, ma200 = ma(120), ma(200)
    ma20_prev, ma60_prev = ma(20, 10), ma(60, 20)
    ma120_prev, ma200_prev = ma(120, 20), ma(200, 20)

    t_open = rt.get("today_open") or price
    t_low = rt.get("today_low") or min(price, t_open)
    change_rate = rt.get("change_rate")
    if change_rate is None:
        change_rate = (price / prev_close - 1) * 100
    today_volume = rt.get("today_volume") or 0
    vwap = rt.get("vwap") or price
    avg_vol20 = (sum(vols[:20]) / len(vols[:20])) or 1
    # v1.1: 장중에는 '지금까지 쌓였어야 할 거래량' 대비로 환산 (09:49에 0.11배로 찍혀 거래량 조건이 항상 미달이던 문제)
    if today_volume and mst == "OPEN":
        frac = max(D.expected_volume_fraction(D.elapsed_market_minutes()), 0.01)
        vol_ratio = today_volume / frac / avg_vol20
    else:
        vol_ratio = today_volume / avg_vol20 if today_volume else 0
    hi20 = max(highs[:20])
    pullback = (price / hi20 - 1) * 100 if hi20 else 0
    d5 = closes[4] if len(closes) >= 5 else closes[-1]
    rate_5d = (price / d5 - 1) * 100
    bull_today = price > t_open
    up_today = price > prev_close

    base.update({"ma20": ma20, "ma60": ma60, "ma120": ma120, "ma200": ma200,
                 "pullback": round(pullback, 1), "vol_ratio": round(vol_ratio, 2),
                 "change_rate": change_rate, "rate_5d": round(rate_5d, 1),
                 "_bear_today": (not bull_today) and (not up_today)})

    # 장기추세 기준선: 200일선이 있으면 그걸로, 없으면 120일선
    long_ma = ma200 if ma200 else ma120
    long_ma_prev = ma200_prev if ma200 else ma120_prev
    long_name = "200일선" if ma200 else "120일선"

    # ── 차단 게이트 ──
    if rt.get("temp_stop"):
        blocks.append("거래정지")
    if rt.get("stat_code") in D.BLOCK_STAT_CODES:
        blocks.append(D.BLOCK_STAT_CODES[rt["stat_code"]])
    if rt.get("warn_code") in D.BLOCK_WARN_CODES:
        blocks.append(D.BLOCK_WARN_CODES[rt["warn_code"]])
    if filings:
        blocks.append("악재공시 " + filings[0])
    if change_rate >= HOT_INTRADAY_BLOCK:
        blocks.append(f"당일 +{change_rate:.1f}% 급등 — 추격 금지")
    if rate_5d <= DROP_5D_BLOCK:
        blocks.append(f"5일 {rate_5d:.1f}% 급락 — 스윙 부적합")
    if pullback <= DROP_FROMHI_BLOCK:
        blocks.append(f"20일 고점 대비 {pullback:.1f}% — 악재성 하락 의심")
    if long_ma and long_ma_prev and price < long_ma and long_ma < long_ma_prev:
        blocks.append(f"떨어지는 칼날 ({long_name} 아래 + {long_name} 하락)")
    elif ma60 and ma60_prev and price < ma60 and ma60 < ma60_prev:
        blocks.append("하락추세 (60일선 아래 + 60일선 하락)")

    # ── ① 정배열/추세 (25) ──
    if ma5 and ma20 and ma60 and ma120 and price > ma5 > ma20 > ma60 > ma120:
        s1 = 25
        signals.append("완전 정배열 (현재가>5>20>60>120일선)")
    elif ma20 and ma60 and price > ma20 > ma60 and ma60_prev and ma60 >= ma60_prev:
        s1 = 18
        signals.append("상승추세 (20>60일선 + 60일선 상승)")
    elif ma60 and price > ma60 and (ma120 is None or price > ma120):
        s1 = 12
        signals.append("60일선 위")
    elif ma20 and price > ma20:
        s1 = 8
    else:
        s1 = 0
    if long_ma and price > long_ma:
        signals.append(f"{long_name} 위")
    bd["①추세"] = s1

    # ── ② 눌림목 위치 (25) — 상승추세 속 지지선 근접 조정 ──
    near20 = ma20 and abs(price / ma20 - 1) <= 0.03
    near60 = ma60 and abs(price / ma60 - 1) <= 0.04
    if -10 <= pullback <= -3 and (near20 or near60):
        s2 = 25
        signals.append(f"눌림목 {pullback:.1f}% + {'20일선' if near20 else '60일선'} 지지권")
    elif -12 <= pullback <= -3:
        s2 = 18
        signals.append(f"조정 구간 {pullback:.1f}%")
    elif -3 < pullback <= 0:
        s2 = 10
        warnings.append(f"고점 바로 아래({pullback:.1f}%) — 눌림 얕음")
    elif pullback > 0:
        if vol_ratio >= 1.5:
            s2 = 15
            signals.append(f"신고가권 돌파 (거래량 {vol_ratio:.1f}배)")
        else:
            s2 = 8
            warnings.append("신고가권이나 거래량 미흡")
    elif -20 <= pullback < -12:
        s2 = 8
        warnings.append(f"깊은 조정 {pullback:.1f}% — 지지 확인 필요")
    else:
        s2 = 0
    bd["②눌림목"] = s2

    # ── ③ 수급 (25) — 대형주의 핵심 동력 ──
    fore3 = (inv or {}).get("fore_3d") or 0
    inst3 = (inv or {}).get("inst_3d") or 0
    prog = rt.get("_pgtr_ntby") or 0
    if fore3 > 0 and inst3 > 0:
        s3 = 20
        signals.append("외국인+기관 3일 동반 순매수 🔥")
    elif fore3 > 0 or inst3 > 0:
        s3 = 12
        signals.append(f"{'외국인' if fore3 > 0 else '기관'} 3일 순매수")
    else:
        s3 = 0
        warnings.append("외국인·기관 수급 부진")
    if prog > 0:
        s3 = min(25, s3 + 5)
        signals.append("프로그램 순매수")
    if fore3 < 0 and inst3 < 0:
        warnings.append("외국인·기관 동반 순매도")
    bd["③수급"] = s3

    # ── ④ 반등 확인 (15) ──
    s4 = 0
    if bull_today and up_today:
        s4 += 8
        signals.append("오늘 양봉 + 전일종가 돌파")
    elif up_today:
        s4 += 4
    elif not bull_today:
        warnings.append("오늘 음봉")
    if vol_ratio >= 1.2:
        s4 += 4
    if vwap and price >= vwap:
        s4 += 2
    if strength and strength >= 100:
        s4 += 1
    s4 = min(15, s4)
    bd["④반등"] = s4

    # ── ⑤ 상대강도 (10) ──
    s5 = 0
    if sector_avg is not None and change_rate > sector_avg:
        s5 += 5
        signals.append("섹터 평균 대비 강세")
    if rate_5d > 0:
        s5 += 5
    bd["⑤상대강도"] = s5

    total = max(0, min(100, s1 + s2 + s3 + s4 + s5))
    base.update({"total": total, "bd": bd})
    return base


# ── 등급 판정 ──
def decide_grade(total, th):
    if total >= th["🚀선도매수"]:
        return "🚀선도매수"
    if total >= th["⬆매수대기"]:
        return "⬆매수대기"
    if total >= th["👀관심"]:
        return "👀관심"
    return "⬇제외"


def lead_disqualifiers(sc):
    """선도매수(최우선 진입) 자격 — 대형주 스윙 품질 조건."""
    why = []
    if sc["bd"].get("①추세", 0) < 18:
        why.append("정배열 미흡")
    if sc["bd"].get("③수급", 0) < 12:
        why.append("수급 부진")
    if sc.get("_bear_today"):
        why.append("오늘 음봉/하락")
    if (sc.get("change_rate") or 0) >= HOT_LEAD_CAUTION:
        why.append(f"당일 +{sc['change_rate']:.1f}% 과열권")
    return why


def finalize(sc, th):
    if sc["blocks"]:
        sc["grade"] = "⛔차단"
        sc["lead_block"] = []
        return sc
    g = decide_grade(sc["total"], th)
    sc["lead_block"] = []
    if g in ("🚀선도매수", "⬆매수대기"):
        why = lead_disqualifiers(sc)
        sc["lead_block"] = why
        if why and g == "🚀선도매수":
            g = "⬆매수대기"
    sc["grade"] = g
    return sc


# ══════════════════════════════════════════════════════════════════
# 스캔 본체
# ══════════════════════════════════════════════════════════════════
def run_scan(p, log=print, progress=None, should_stop=None, write_excel=True):
    D.init_feeds()
    uni = build_universe_daejang(p["mcap_min"], p["mcap_max"], p["min_amount"], int(p["top_n"]), log=log)
    if not uni:
        log("  조건에 맞는 대장주가 없음.\n")
        return [], [], {}
    mst = D.market_status()
    cache = D.DayCache()
    today = datetime.now().strftime("%Y%m%d")
    log(f"  스캔 시작 — 대상 {len(uni)}개 (일봉 200개씩 조회라 수 분 걸릴 수 있어)\n")

    collected = []
    for i, st in enumerate(uni, 1):
        if should_stop and should_stop():
            log("  중단됨.\n")
            break
        code = st["code"]
        try:
            hist = cache.get_or(f"sd:{code}", lambda: fetch_daily_deep(code))
            rt = D.fetch_realtime(code)
            inv = cache.get_or(f"i:{code}", lambda: D._safe(D.fetch_investor, code, None)) or {}
            fil = cache.get_or(f"f:{code}", lambda: D._safe(D.fetch_filings, code, None)) or []
            if hist and rt:
                st["sector"] = rt.get("sector") or st.get("sector") or "기타"
                collected.append((st, hist, rt, inv, fil))
            else:
                log(f"  ✗ {st['name']}({code}) 시세 없음\n")
        except D.KISError as e:
            log(f"  ✗ {st['name']}({code}) {e}\n")
        if progress:
            progress(i, len(uni), st["name"])
    cache.save()
    if not collected:
        return [], [], {}

    # 섹터 평균 등락률 (상대강도용)
    sec = {}
    for st, _, rt, _, _ in collected:
        sec.setdefault(st["sector"], []).append(rt.get("change_rate") or 0)
    sec_avg = {k: sum(v) / len(v) for k, v in sec.items() if len(v) >= 2}

    # 지수 게이트로 임계값 보수화 (GUI에서 p["grade"]로 기준 조절 가능)
    gb = p.get("grade") or GRADE_BASE
    mgate = D.assess_market_gate()
    extra = min(20, mgate.get("extra", 0) or 0)
    th = {k: v + extra for k, v in gb.items()}

    results = []
    for st, hist, rt, inv, fil in collected:
        if should_stop and should_stop():
            break
        pre = score_swing(hist, rt, inv, None, fil, sec_avg.get(st["sector"]))
        strength = None
        if mst == "OPEN" and not pre["blocks"] and pre["total"] >= th["👀관심"] - 8:
            strength = D._safe(D.fetch_strength, st["code"], None)
        sc = score_swing(hist, rt, inv, strength, fil, sec_avg.get(st["sector"]))
        price = rt["price"]
        plan = D.trade_plan(price, [d for d in hist if d["date"] < today], p["stop"], rt.get("today_low"))
        sc = finalize(sc, th)
        results.append({
            "name": st["name"], "code": st["code"], "sector": st["sector"], "grade": sc["grade"],
            "score": sc["total"], "price": price, "change_rate": sc["change_rate"], "mcap": sc["mcap"],
            "ma20": sc["ma20"], "ma60": sc["ma60"], "ma120": sc["ma120"], "ma200": sc["ma200"],
            "pullback": sc["pullback"], "vol_ratio": sc["vol_ratio"], "vwap": sc["vwap"],
            "strength": sc["strength"], "fore_3d": sc["fore_3d"], "inst_3d": sc["inst_3d"],
            "prog_net": sc["prog_net"], "rate_5d": sc["rate_5d"], "plan": plan,
            "stop": plan["stop"], "target": plan["target"], "atr_pct": plan.get("atr_pct"),
            "signals": sc["signals"], "warnings": sc["warnings"], "blocks": sc["blocks"],
            "lead_block": sc.get("lead_block", []), "bd": sc["bd"],
            "rets20": D.daily_returns([d for d in hist if d["date"] < today]),
        })

    n_lead = D.cap_lead_count(results, int(p["positions"]) * 2)
    order = {"🚀선도매수": 0, "⬆매수대기": 1, "👀관심": 2, "⬇제외": 3, "⛔차단": 4}
    results.sort(key=lambda r: (order.get(r["grade"], 9), -r["score"]))
    if mgate.get("block"):                      # 지수 하락 구간엔 신규 스윙 진입 중단 (임계값 상향만으로는 부족)
        portfolio = []
        log(f"  ⛔ 신규 진입 중단 — {mgate.get('block_reason')} (오늘은 포트폴리오 없음, 관찰만)\n")
    else:
        portfolio = build_portfolio(results, p)

    regime = {"label": mgate.get("label", "-"), "gate": mgate, "thresholds": th, "n_lead": n_lead}
    log(f"  완료 — 조회 {len(collected)}개 / 선도 {n_lead}개 / KIS 호출 {D._KIS.calls}회\n")
    log(f"  지수 게이트: {mgate.get('desc', '')}\n")
    log(f"  등급 기준: 선도≥{th['🚀선도매수']:.0f} 대기≥{th['⬆매수대기']:.0f} 관심≥{th['👀관심']:.0f}\n")

    if write_excel:
        try:
            write_sheets(SCRIPT_DIR / "대장주스윙_기록.xlsx", results, portfolio, regime, p)
            log("  엑셀 기록: 대장주스윙_기록.xlsx\n")
        except PermissionError:
            log("  ✗ 엑셀이 열려 있어 기록 못 함 — 닫고 다시 스캔해줘.\n")
        except Exception as e:
            log(f"  ✗ 엑셀 기록 실패: {e}\n")
    return results, portfolio, regime


def build_portfolio(results, p):
    """선도매수 + (품질통과)매수대기 중 섹터당 1개 · 서로 상관 낮은 종목만, 목표 보유수만큼."""
    def ok(r):
        return not [x for x in (r.get("lead_block") or []) if not x.startswith("선도 정원")]
    prim = sorted([r for r in results if r["grade"] == "🚀선도매수"], key=lambda r: -r["score"]) + \
        sorted([r for r in results if r["grade"] == "⬆매수대기" and ok(r)], key=lambda r: -r["score"])
    picks = D.pick_diversified(prim, int(p["positions"]))      # 섹터당 1개 + 같이 움직이는 종목 중복 금지
    rows = []
    for r in picks:
        row = D.size_position(r, p["capital"], int(p["positions"]), p["stop"], p["stop"] * D.REWARD_RISK, p["risk"])
        if row:
            rows.append(row)
    return rows


# ══════════════════════════════════════════════════════════════════
# 엑셀 기록
# ══════════════════════════════════════════════════════════════════
_F_NAVY = PatternFill("solid", fgColor="1B3A5C")
_F_YELL = PatternFill("solid", fgColor="FFF2CC")
_F_WARN = PatternFill("solid", fgColor="FFC7CE")
_F_OK = PatternFill("solid", fgColor="C6EFCE")
_HDR = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=9)
_DATA = Font(name="맑은 고딕", size=9)
_CEN = Alignment(horizontal="center", vertical="center")


def _sheet(wb, name, idx=None):
    if name in wb.sheetnames:
        ws = wb[name]
        ws.delete_rows(1, ws.max_row)
        return ws
    return wb.create_sheet(name, idx) if idx is not None else wb.create_sheet(name)


def write_sheets(path, results, portfolio, regime, p):
    wb = openpyxl.load_workbook(path) if Path(path).exists() else openpyxl.Workbook()
    if "Sheet" in wb.sheetnames and wb["Sheet"].max_row == 1 and wb["Sheet"].max_column == 1:
        wb.remove(wb["Sheet"])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # (1) 대장주스윙 추천
    ws = _sheet(wb, "🎯대장주스윙", 0)
    ws.cell(1, 1, f"🎯 대장주 단기스윙 — KIS 실시간   |   지수게이트: {regime.get('label', '-')}   |   {now_str}")
    ws.cell(1, 1).font = Font(name="맑은 고딕", bold=True, size=12, color="FFFFFF")
    ws.cell(1, 1).fill = _F_NAVY
    heads = ["순위", "종목명", "코드", "섹터", "점수", "판정", "현재가", "등락%", "시총(억)",
             "눌림%", "거래량배", "외인3일", "기관3일", "ATR%", "손절가", "목표가", "선도제외", "매수근거"]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(3, c, h)
        cell.font = _HDR; cell.fill = _F_NAVY; cell.alignment = _CEN
    rk = 0
    for r in results:
        if r["grade"] in ("⬇제외", "⛔차단"):
            continue
        rk += 1
        vals = [rk, r["name"], r["code"], r["sector"], round(r["score"]), r["grade"], r["price"],
                round(r["change_rate"] or 0, 2), r["mcap"], r["pullback"], r["vol_ratio"],
                r["fore_3d"], r["inst_3d"], r["atr_pct"], r["stop"], r["target"],
                " / ".join(r.get("lead_block") or []), " | ".join(r["signals"][:4])]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(rk + 3, c, v)
            cell.font = _DATA; cell.alignment = _CEN
            if r["grade"] == "🚀선도매수":
                cell.fill = _F_OK
            elif r["grade"] == "⬆매수대기":
                cell.fill = _F_YELL
    for c, w in enumerate([5, 16, 9, 12, 6, 11, 9, 7, 9, 7, 8, 9, 9, 7, 9, 9, 22, 60], 1):
        ws.column_dimensions[chr(64 + c)].width = w

    # (2) 차단 종목
    ws = _sheet(wb, "⛔차단", 1)
    ws.cell(1, 1, f"⛔ 진입 차단 (점수 무관 매수 금지)   |   {now_str}").font = Font(name="맑은 고딕", bold=True, size=11)
    for c, h in enumerate(["종목명", "코드", "섹터", "현재가", "등락%", "차단 사유"], 1):
        cell = ws.cell(3, c, h); cell.font = _HDR; cell.fill = _F_NAVY; cell.alignment = _CEN
    r0 = 4
    for r in results:
        if r["grade"] != "⛔차단":
            continue
        for c, v in enumerate([r["name"], r["code"], r["sector"], r["price"],
                               round(r["change_rate"] or 0, 2), " / ".join(r["blocks"])], 1):
            cell = ws.cell(r0, c, v); cell.font = _DATA; cell.fill = _F_WARN
        r0 += 1
    for c, w in enumerate([16, 9, 12, 10, 8, 70], 1):
        ws.column_dimensions[chr(64 + c)].width = w

    # (3) 포지션 추천
    ws = _sheet(wb, "포지션추천", 2)
    ws.cell(1, 1, f"💰 오늘의 스윙 포트폴리오 (자본 {p['capital']:,.0f}원 · 손절상한 {p['stop']:.0f}% · {now_str})").font = \
        Font(name="맑은 고딕", bold=True, size=11)
    ph = ["종목명", "코드", "섹터", "판정", "점수", "현재가", "수량", "투입금액",
          "손절가(자동주문)", "목표가(자동주문)", "손절%", "목표%", "ATR%", "최대손실", "최대이익"]
    for c, h in enumerate(ph, 1):
        cell = ws.cell(3, c, h); cell.font = _HDR; cell.fill = _F_NAVY; cell.alignment = _CEN
    for i, r in enumerate(portfolio, 4):
        vals = [r["name"], r["code"], r["sector"], r["grade"], round(r["score"]), r["price"], r["shares"],
                r["invested"], r["stop_price"], r["target_price"], r["stop_pct"], r["target_pct"],
                r.get("atr_pct"), round(r["max_loss"]), round(r["max_gain"])]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(i, c, v); cell.font = _DATA; cell.fill = _F_YELL; cell.alignment = _CEN
    for c, w in enumerate([16, 9, 12, 11, 6, 9, 7, 11, 14, 14, 7, 7, 7, 10, 10], 1):
        ws.column_dimensions[chr(64 + c)].width = w

    wb.save(path)


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════
def _print_summary(results, portfolio, regime):
    cand = [r for r in results if r["grade"] not in ("⬇제외", "⛔차단")]
    print("\n" + "═" * 70)
    print(f"대장주 단기스윙 — 후보 {len(cand)}개 (선도 {regime.get('n_lead', 0)}개) / 지수 {regime.get('label', '-')}")
    print("═" * 70)
    for r in cand[:15]:
        print(f"  {r['grade']} {r['name']}({r['code']}) {round(r['score'])}점 "
              f"{r['price']:,}원 ({r['change_rate']:+.1f}%) 눌림{r['pullback']}% "
              f"손절{r['stop']:,}/목표{r['target']:,}")
        if r["signals"]:
            print(f"      → {' | '.join(r['signals'][:3])}")
    if portfolio:
        print("\n  [오늘의 추천 포트폴리오]")
        tot = sum(x["invested"] for x in portfolio)
        loss = sum(x["max_loss"] for x in portfolio)
        for r in portfolio:
            print(f"    {r['name']} {r['shares']}주 {r['invested']:,.0f}원 "
                  f"손절{r['stop_price']:,}(-{r['stop_pct']:.1f}%) 목표{r['target_price']:,}(+{r['target_pct']:.1f}%)")
        print(f"    투입 {tot:,.0f}원 / 전부 손절 시 -{loss:,.0f}원")
    else:
        print("\n  조건 충족 종목 없음 — 현금 보유가 전략")
    print("═" * 70)


def _mk_hist(closes):
    """최신→과거 종가 리스트 → 일봉 dict 리스트."""
    hist = []
    for i, c in enumerate(closes):
        d = (datetime.now() - timedelta(days=i + 1)).strftime("%Y%m%d")
        hist.append({"date": d, "open": int(c), "high": int(c * 1.012), "low": int(c * 0.988),
                     "close": int(c), "volume": 1_000_000, "amount": int(c) * 1_000_000})
    return hist


def selftest():
    """KIS 없이 채점 로직 점검 — 정배열 눌림목=고득점, 하락칼날=차단인지."""
    print("[셀프테스트] KIS 연결 없이 채점 로직만 점검\n")

    # ① 이상적 스윙: 가파른 상승추세(최신봉이 고가) + 최근 4봉 눌림 → 오늘 20일선 지지 반등
    peak = 62000
    closes = [peak * (0.994 ** j) for j in range(210)]   # j=0 최신=최고, 과거로 갈수록 하락 = 상승추세
    for j in range(4):                                   # 최근 4봉 완만한 눌림
        closes[j] = closes[4] * (1 - 0.012 * (4 - j))
    hist = _mk_hist(closes)
    prev_close = int(closes[0])
    price = int(closes[0] * 1.02)                        # 오늘 20일선 위로 반등 +2%
    rt = {"price": price, "prev_close": prev_close, "today_open": prev_close,
          "today_high": price, "today_low": prev_close, "change_rate": 2.0,
          "today_volume": 1_400_000, "vwap": prev_close, "_mcap": 200000,
          "_pgtr_ntby": 50000, "stat_code": "", "warn_code": "", "temp_stop": False, "sector": "전기전자"}
    inv = {"fore_3d": 120000, "inst_3d": 80000}
    sc = finalize(score_swing(hist, rt, inv, strength=115, filings=[], sector_avg=0.3), GRADE_BASE)
    print(f"  상승추세+눌림+쌍끌이수급: {sc['total']}점 {sc['grade']}  (칼날차단={bool(sc['blocks'])})")
    print(f"    MA20={sc['ma20']:.0f} MA60={sc['ma60']:.0f} MA120={sc['ma120']:.0f} 현재가={price} 눌림={sc['pullback']}%")
    print(f"    점수분해: {sc['bd']}")
    assert not sc["blocks"], f"정상 상승추세인데 차단됨: {sc['blocks']}"
    assert sc["grade"] in ("🚀선도매수", "⬆매수대기") and sc["total"] >= 60, "이상적 셋업인데 등급 미달"

    # ② 하락 칼날: 장기 하락추세(최신봉이 최저) → 차단돼야 함
    closes2 = [50000 * (1.004 ** j) for j in range(210)]   # j=0 최신=최저, 과거가 더 높음 = 하락추세
    hist2 = _mk_hist(closes2)
    rt2 = {**rt, "price": int(closes2[0]), "prev_close": int(closes2[0]), "today_open": int(closes2[0]),
           "today_high": int(closes2[0]), "today_low": int(closes2[0] * 0.98), "change_rate": -2.0,
           "_pgtr_ntby": -50000}
    sc2 = finalize(score_swing(hist2, rt2, {"fore_3d": -100000, "inst_3d": -50000}, None, [], -0.5), GRADE_BASE)
    print(f"\n  장기 하락추세(칼날): {sc2['total']}점 {sc2['grade']}")
    print(f"    차단사유: {sc2['blocks']}")
    assert sc2["grade"] == "⛔차단", "하락 칼날인데 차단이 안 됨 — 로직 점검 필요"
    print("\n[셀프테스트 통과] ✅ 상승추세 눌림목=후보, 하락칼날=차단")


def main():
    ap = argparse.ArgumentParser(description="대장주 단기스윙 스캐너 v1")
    ap.add_argument("--capital", type=float, default=DEFAULTS["capital"])
    ap.add_argument("--positions", type=int, default=DEFAULTS["positions"])
    ap.add_argument("--stop", type=float, default=DEFAULTS["stop"])
    ap.add_argument("--risk", type=float, default=DEFAULTS["risk"])
    ap.add_argument("--mcap-min", type=float, default=DEFAULTS["mcap_min"])
    ap.add_argument("--mcap-max", type=float, default=DEFAULTS["mcap_max"])
    ap.add_argument("--min-amount", type=float, default=DEFAULTS["min_amount"])
    ap.add_argument("--top", type=int, default=DEFAULTS["top_n"])
    ap.add_argument("--no-excel", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    p = {"capital": a.capital, "positions": a.positions, "stop": a.stop, "risk": a.risk,
         "mcap_min": a.mcap_min, "mcap_max": a.mcap_max, "min_amount": a.min_amount, "top_n": a.top}
    try:
        results, portfolio, regime = run_scan(p, write_excel=not a.no_excel)
        _print_summary(results, portfolio, regime)
    except D.KISError as e:
        print(f"\n[KIS 오류] {e}\n키(kis_config.json)와 실전/모의 설정을 확인해줘.")


if __name__ == "__main__":
    main()
