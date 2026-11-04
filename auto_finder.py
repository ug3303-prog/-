# -*- coding: utf-8 -*-
"""
auto_finder.py — 대장주 자동 발굴기

조건 3가지를 모두 만족하는 종목을 시장에서 자동으로 찾아 올려준다:
  ① 대형주 (시가총액 상위 / 하한 이상)
  ② 상승추세 (현재가 > 200일 이동평균)
  ③ 눌림 (최근 20거래일 고점 대비 -X% 이상)
그리고 조건 충족 종목 중 점수가 높은 3종목(추천 후보)을 따로 뽑아준다.

※ 추천이 아니라 '조건 스크리닝'이다. 매수 여부·종목·금액은 본인 판단.
※ 대형주만 대상. 초소형 급등락은 이 도구 범위 밖.

준비: pip install -r requirements.txt   (kis_feed.py, dart_feed.py 도 같은 폴더에)
사용:
  CLI  : python auto_finder.py
  GUI  : python gui.py
  코드에서: run_scan(...) 을 호출 (gui.py 참고)
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import kis_feed as kf      # _get, _throttle, _i, _f, fetch_realtime, fetch_investor_flow, _load_keys 재사용
import dart_feed as dart   # DART 전자공시 — 부채비율/영업이익 등 재무 펀더멘털

# ── 발굴 조건 기본값 (GUI/run_scan 에서 override 가능) ──────────────
TOP_N        = 80       # 시총 상위 몇 개를 훑을지 (많을수록 느림)
MIN_MCAP_억  = 20_000   # 최소 시가총액(억원). 2조 미만 제외 = 진짜 대형주만
PULLBACK_PCT = 5.0      # 눌림 기준(%). 20일 고점 대비
MAX_PULLBACK_PCT = 0    # 눌림 상한(%). 0 = 제한 없음. (예: 20 → 20% 넘게 빠진 종목은 악재 의심으로 제외)
LOOKBACK     = 20       # 고점 산정 거래일
MA_PERIOD    = 200      # 추세 판단 이동평균 (현재가 > 200일선 = 상승추세)
MAX_SHOW     = 25       # 결과 최대 표시 개수
MIN_AVG_TRADE_VALUE_억 = 30   # 최소 평균거래대금(억원). LOOKBACK일 평균, 유동성 없는 종목 제외
MAX_PER      = 0        # PER 상한 (0 = 필터 안 함). 적자 등 PER 없는 종목은 필터 대상에서 제외하지 않음
MAX_PBR      = 0        # PBR 상한 (0 = 필터 안 함)
MAX_DEBT_RATIO    = 0   # DART 부채비율(%) 상한 (0 = 필터 안 함). 금융업은 면제
REQUIRE_OP_PROFIT = False  # True면 최근 영업이익 적자 종목 제외
USE_DART          = False # True면 종목마다 DART 재무정보를 추가 조회(스캔이 느려짐)
REQUIRE_MA_RISING = False # True면 200일선이 20거래일 전보다 올라 있는 종목만 '충족'으로 인정
USE_FLOW          = True  # True면 추천 후보 상위 종목의 외국인/기관 5일 순매수를 조회
PICK_N            = 3     # 최종 추천 개수

# ── 추천(3종목) 하드 필터 / 점수 기준 ─────────────────────────────
PICK_MIN_PULLBACK = 7.0    # 이보다 덜 빠졌으면 '눌림'으로 보기 어려움
PICK_MAX_PULLBACK = 20.0   # 판단 기준 문서: 20%+ 는 악재 의심
PICK_MIN_ABOVE_MA = 3.0    # 200일선을 막 넘은 종목은 추세 전환 리스크
PICK_MAX_ABOVE_MA = 60.0   # 너무 멀면 추격 부담
PICK_MAX_VOL_RATIO = 2.5   # 거래량이 평소의 2.5배 넘으면 뭔가 터진 것
MA_SLOPE_DAYS = 20         # 이동평균 기울기 비교 기간(거래일)

_BASE_DIR = Path(__file__).parent
_MASTER_CACHE = _BASE_DIR / ".krx_master_cache.json"
_PRICE_CACHE_DIR = _BASE_DIR / ".price_cache"
_SNAPSHOT_FILE = _BASE_DIR / ".last_scan.json"
_PRICE_CACHE_FULL_TTL = 7 * 24 * 3600   # 일봉은 일주일에 한 번 전체 재수집(수정주가 변동 대응)

_KST = timezone(timedelta(hours=9))


def _now_kst() -> datetime:
    return datetime.now(_KST)


def is_intraday(now: datetime | None = None) -> bool:
    """정규장 진행 중(평일 09:00~15:40 KST)이면 True — 오늘 일봉이 미완성이라는 뜻."""
    now = now or _now_kst()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm < 15 * 60 + 40


# ═════════════════════════════════════════════════════════════════
#  결과 자료형
# ═════════════════════════════════════════════════════════════════
@dataclass
class Candidate:
    name: str
    code: str
    mcap: int
    price: int
    pullback: float
    above_ma: float
    trade_value: float            # 평균거래대금(억)
    vol_ratio: float
    ma_slope: float | None = None  # 200일선 20거래일 변화율(%)
    sector: str = ""
    per: float | None = None
    pbr: float | None = None
    per_loss: bool = False         # PER 없음 = 적자
    per_rel: float | None = None   # 업종 중앙값 대비 PER(%)
    pbr_rel: float | None = None
    debt_ratio: float | None = None
    op_yoy: float | None = None
    rev_yoy: float | None = None
    op_positive: bool | None = None
    op_turnaround: bool = False
    fin_period: str = ""
    is_financial: bool = False
    flow_foreign: float | None = None
    flow_orgn: float | None = None
    flow_unit: str = ""
    is_new: bool = False
    partial_day: bool = False
    score: float | None = None
    reasons: list = field(default_factory=list)


@dataclass
class ScanResult:
    hits: list
    knives: list
    picks: list
    scanned: int
    universe_len: int
    skipped_short_history: int = 0
    skipped_risk: int = 0
    intraday: bool = False
    stopped: bool = False
    as_of: str = ""
    exited: list = field(default_factory=list)   # 직전 스캔에는 있었으나 이번엔 빠진 종목명
    params: dict = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════
#  표시용 포맷터 (GUI/CLI/CSV 공용)
# ═════════════════════════════════════════════════════════════════
def fmt_yoy(v, turnaround=False):
    """증감률 표시. 기저효과로 의미 없는 극단값은 N/M, 적자→흑자는 '흑전'."""
    if turnaround:
        return "흑전"
    if v is None:
        return "-"
    if abs(v) > 500:
        return "N/M"
    return f"{v:+.1f}"


def fmt_flow(v, unit):
    if v is None:
        return "-"
    if unit == "억":
        return f"{v:+,.0f}억"
    return f"{v / 1000:+,.0f}천주"


def fmt_per(c: Candidate):
    if c.per_loss:
        return "적자"
    return f"{c.per:.1f}" if c.per is not None else "-"


# ═════════════════════════════════════════════════════════════════
#  종목 마스터 (대형주 유니버스)
# ═════════════════════════════════════════════════════════════════
_ETF_PREFIXES = ("KODEX", "TIGER", "ACE", "RISE", "SOL ", "PLUS", "HANARO", "KBSTAR", "ARIRANG",
                 "KOSEF", "TIMEFOLIO", "WOORI", "TRUSTON", "HK ", "1Q ", "BNK ")


def _to_int(v):
    if v is None:
        return None
    try:
        s = str(v).strip().replace(",", "")
        if s in ("", "nan", "None"):
            return None
        return int(float(s))
    except Exception:
        return None


def _pick_marcap_df(log):
    """시총이 채워진 KRX 목록 DataFrame. 당일 데이터가 비어 있으면 이전 영업일 캐시로 거슬러 올라간다."""
    import pandas as pd
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX-MARCAP")
    cols = {c.lower(): c for c in df.columns}
    mcap_col = cols.get("marcap") or cols.get("markcap") or cols.get("marketcap")
    if mcap_col and df[mcap_col].notna().mean() >= 0.5:
        return df

    # fdr.StockListing('KRX-MARCAP')은 GitHub에 캐시된 스냅샷 CSV를 쓰는데, 이 캐시가 당일 데이터를
    # 아직 못 채운 경우 Close/Marcap이 비어 있다. KRX 서버 직접 조회는 사내망 등에서 막히는 경우가 많아,
    # GitHub 캐시를 하루씩 거슬러 올라가며 데이터가 채워진 날짜를 찾는다.
    d = datetime.now() - timedelta(days=1)
    for _ in range(10):
        date_str = d.strftime("%Y-%m-%d")
        try:
            candidate = pd.read_csv(
                f"https://raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/"
                f"refs/heads/master/data/listing/krx/{date_str}.csv",
                index_col=0,
                dtype={"Code": str, "Dept": str, "ChangeCode": str, "MarketId": str},
            ).reset_index(drop=True)
        except Exception:
            d -= timedelta(days=1)
            continue
        c_cols = {c.lower(): c for c in candidate.columns}
        c_mcap = c_cols.get("marcap") or c_cols.get("markcap") or c_cols.get("marketcap")
        if c_mcap and candidate[c_mcap].notna().mean() >= 0.5:
            log(f"[참고] 당일 시총 데이터가 비어있어 {date_str}(전 영업일) 마감 기준으로 대체합니다.")
            return candidate
        d -= timedelta(days=1)

    raise RuntimeError(
        "최근 10일 내 시총 데이터를 찾지 못했습니다. FinanceDataReader/KRX 쪽 문제일 수 있으니 잠시 후 다시 시도하세요."
    )


def _df_to_records(df):
    """KRX 목록 DataFrame → 종목 마스터 레코드(dict) 리스트. 시가총액 단위는 억원(문자열)."""
    cols = {c.lower(): c for c in df.columns}
    code_col = cols.get("code") or cols.get("symbol")
    name_col = cols.get("name")
    mcap_col = cols.get("marcap") or cols.get("markcap") or cols.get("marketcap")
    market_col, dept_col, vol_col = cols.get("market"), cols.get("dept"), cols.get("volume")
    if not code_col or not name_col or not mcap_col:
        raise RuntimeError(f"컬럼 확인 필요: {list(df.columns)}")

    # 거래량 0 = 거래정지로 볼 수 있지만, 데이터가 비정상이라 대부분 0이면 이 휴리스틱은 쓰지 않는다.
    use_halt = False
    if vol_col:
        vols = df[vol_col].dropna()
        use_halt = len(vols) > 0 and (vols == 0).mean() < 0.3

    records = []
    for row in df.to_dict("records"):
        mcap_won = row.get(mcap_col)
        try:
            mcap_억 = int(float(mcap_won) / 1e8) if mcap_won == mcap_won and mcap_won else None
        except Exception:
            mcap_억 = None
        if mcap_억 is None:
            continue   # 시총을 모르면 '대형주' 조건을 확인할 수 없으므로 제외
        code = str(row.get(code_col)).strip()
        name = str(row.get(name_col)).strip()
        dept = str(row.get(dept_col) or "") if dept_col else ""
        group = "ST"
        if name.startswith(_ETF_PREFIXES):
            group = "EF"
        elif "리츠" in name:
            group = "RT"
        vol = row.get(vol_col) if vol_col else None
        records.append({
            "code": code,
            "name": name,
            "market": str(row.get(market_col) or "") if market_col else "",
            "그룹코드": group,
            "SPAC": "Y" if ("스팩" in name or "기업인수목적" in name) else "N",
            "거래정지": "Y" if (use_halt and vol is not None and vol == 0) else "N",
            "정리매매": "N",
            "관리종목": "Y" if ("관리" in dept or "환기" in dept) else "N",
            "시장경고": "00",   # 이 데이터 소스엔 없음 — 후보 단계에서 KIS 현재가 응답으로 재확인
            "우선주": "0" if (code and code[-1] == "0") else "1",   # 보통주 코드는 0으로 끝남
            "시가총액": str(mcap_억),
        })
    return records


def _download_master_records(log_cb=None):
    def log(msg):
        if log_cb:
            log_cb(msg)
    df = _pick_marcap_df(log)
    return _df_to_records(df)


def _load_master_records(log_cb=None):
    """종목 마스터. 오늘 받아 둔 캐시가 있으면 그것을, 없으면 새로 받고, 실패하면 저장된 옛 목록을 쓴다."""
    def log(msg):
        if log_cb:
            log_cb(msg)

    cache = None
    try:
        cache = json.loads(_MASTER_CACHE.read_text(encoding="utf-8"))
    except Exception:
        cache = None

    today = _now_kst().strftime("%Y-%m-%d")
    if cache and cache.get("as_of") == today and cache.get("records"):
        return cache["records"]

    try:
        records = _download_master_records(log_cb)
        if not records:
            raise RuntimeError("종목 목록이 비어 있습니다")
        try:
            _MASTER_CACHE.write_text(
                json.dumps({"as_of": today, "records": records}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        return records
    except Exception as e:
        if cache and cache.get("records"):
            log(f"[참고] 종목 목록 다운로드 실패({e}) — 저장된 목록({cache.get('as_of', '?')} 기준)을 사용합니다.")
            return cache["records"]
        raise


def load_top_marcap(top_n: int, min_mcap_억: int, log_cb=None):
    """KRX 시총 상위 종목. 반환: [(code6, name, mcap억), ...]
    제외: 비보통주(ETF/리츠 등), 스팩, 우선주, 거래정지, 정리매매, 관리종목, 시장경고(02·03)."""
    records = _load_master_records(log_cb)
    out = []
    for r in records:
        mcap = _to_int(r.get("시가총액"))
        if mcap is None or mcap < min_mcap_억:
            continue
        if str(r.get("그룹코드", "ST")) != "ST":
            continue
        if str(r.get("SPAC", "N")) == "Y":
            continue
        if str(r.get("거래정지", "N")) == "Y" or str(r.get("정리매매", "N")) == "Y" or str(r.get("관리종목", "N")) == "Y":
            continue
        if str(r.get("시장경고", "00")) in ("02", "03"):
            continue
        if str(r.get("우선주", "0")) not in ("0", ""):
            continue
        out.append((str(r["code"]), str(r["name"]), mcap))
    out.sort(key=lambda x: x[2], reverse=True)
    return out[:top_n]


# ═════════════════════════════════════════════════════════════════
#  일봉 수집 (+ 디스크 캐시)
# ═════════════════════════════════════════════════════════════════
def _bar_from_item(it):
    d = it.get("stck_bsop_date")
    c = kf._i(it.get("stck_clpr"))
    if not d or c is None:
        return None
    vol = kf._i(it.get("acml_vol"), 0)
    tv = kf._f(it.get("acml_tr_pbmn"))   # 누적 거래대금(원). 없으면 거래량×종가로 근사
    return {
        "date": d,
        "high": kf._i(it.get("stck_hgpr"), 0),
        "low": kf._i(it.get("stck_lwpr"), 0),
        "close": c,
        "volume": vol,
        "value": tv if (tv is not None and tv > 0) else float(vol * c),
    }


def _fetch_range(code6: str, start: datetime, end: datetime):
    j = kf._get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        tr_id="FHKST03010100",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code6,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        },
    )
    arr = (j or {}).get("output2") or []
    return [b for b in (_bar_from_item(it) for it in arr) if b]


def _load_price_cache(code6):
    try:
        return json.loads((_PRICE_CACHE_DIR / f"{code6}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_price_cache(code6, bars_by_date, full_fetched_at):
    try:
        _PRICE_CACHE_DIR.mkdir(exist_ok=True)
        (_PRICE_CACHE_DIR / f"{code6}.json").write_text(
            json.dumps({"full_fetched_at": full_fetched_at, "bars": bars_by_date}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _fetch_history(code6: str, min_rows: int = 230, lookback: int = LOOKBACK, use_cache: bool = True):
    """
    200일선 계산용 장기 일봉. KIS 일봉은 1회 ~100개라 날짜창을 뒤로 밀며 모은다.
    디스크 캐시가 있으면 최근 구간만 새로 받아 합친다(겹치는 구간의 종가가 달라졌으면 — 액면분할/배당
    조정 등 — 캐시를 버리고 전체 재수집).
    반환: 최신순 [{date, high, low, close, volume, value}, ...]  (실패 시 None)
    """
    now = _now_kst()
    today = now.strftime("%Y%m%d")
    now_naive = now.replace(tzinfo=None)

    collected = {}
    full_fetched_at = time.time()

    cache = _load_price_cache(code6) if use_cache else None
    if cache and cache.get("bars") and time.time() - cache.get("full_fetched_at", 0) < _PRICE_CACHE_FULL_TTL:
        cached = cache["bars"]
        if len(cached) >= min_rows:
            last = max(cached)
            start = datetime.strptime(last, "%Y%m%d") - timedelta(days=7)
            fresh = _fetch_range(code6, start, now_naive)
            mismatch = any(
                b["date"] in cached and b["date"] != today
                and abs(cached[b["date"]]["close"] - b["close"]) / max(b["close"], 1) > 0.005
                for b in fresh
            )
            if fresh and not mismatch:
                collected = dict(cached)
                for b in fresh:
                    collected[b["date"]] = b
                full_fetched_at = cache.get("full_fetched_at", time.time())

    if not collected:
        end = now_naive
        for _ in range(5):  # 최대 5회 (~450 영업일)
            start = end - timedelta(days=130)
            bars = _fetch_range(code6, start, end)
            if not bars:
                break
            for b in bars:
                collected[b["date"]] = b
            if len(collected) >= min_rows:
                break
            end = datetime.strptime(min(b["date"] for b in bars), "%Y%m%d") - timedelta(days=1)
            kf._throttle()

    if len(collected) < lookback:
        return None

    if use_cache:
        persist = {d: b for d, b in collected.items() if not (d == today and is_intraday(now))}
        _save_price_cache(code6, persist, full_fetched_at)

    return sorted(collected.values(), key=lambda x: x["date"], reverse=True)


# ═════════════════════════════════════════════════════════════════
#  지표 계산 (순수 함수 — API 없이 테스트 가능)
# ═════════════════════════════════════════════════════════════════
def compute_metrics(daily, lookback: int, ma_period: int, intraday: bool = False, today: str | None = None):
    """
    daily: 최신순 일봉 리스트. 반환 dict 또는 None(데이터 부족).
    장중(intraday)이고 daily[0]이 오늘 봉이면 그 봉은 미완성이므로 거래량/거래대금/이동평균 계산에서 뺀다
    (고점 계산에는 오늘 장중 고가를 포함).
    """
    if not daily or len(daily) < lookback:
        return None
    partial = bool(intraday and today and daily[0]["date"] == today)
    complete = daily[1:] if partial else daily
    if len(complete) < lookback:
        return None

    high_n = max(d["high"] for d in daily[:lookback])
    stat = complete[:lookback]
    avg_vol = sum(d["volume"] for d in stat) / len(stat)
    avg_tv = sum((d.get("value") or d["volume"] * d["close"]) for d in stat) / len(stat) / 1e8
    vol_ratio = (complete[0]["volume"] / avg_vol) if avg_vol else 0.0

    closes = [d["close"] for d in complete]
    ma_ok = len(closes) >= ma_period
    ma_val = sum(closes[:ma_period]) / ma_period if ma_ok else None
    ma_slope = None
    if ma_ok and len(closes) >= ma_period + MA_SLOPE_DAYS:
        prev = sum(closes[MA_SLOPE_DAYS:MA_SLOPE_DAYS + ma_period]) / ma_period
        if prev:
            ma_slope = (ma_val / prev - 1) * 100

    return {
        "high_n": high_n,
        "avg_trade_value": avg_tv,
        "vol_ratio": vol_ratio,
        "ma_ok": ma_ok,
        "ma_val": ma_val,
        "ma_slope": ma_slope,
        "partial": partial,
        "last_close": daily[0]["close"],
    }


_FIN_KEYWORDS = ("은행", "금융", "보험", "증권", "카드", "캐피탈", "저축", "종금")


def is_financial(sector: str = "", debt_ratio: float | None = None, name: str = "") -> bool:
    """금융업 여부. 금융업은 부채비율이 구조적으로 1,000%대라 부채비율 필터에서 면제한다."""
    if sector:
        return any(k in sector for k in _FIN_KEYWORDS)
    if any(k in name for k in ("금융지주", "은행", "증권", "생명", "화재", "보험", "카드")):
        return True
    return debt_ratio is not None and debt_ratio >= 800


# ═════════════════════════════════════════════════════════════════
#  점수화 / 3종목 선정
# ═════════════════════════════════════════════════════════════════
def _trap(x, a, b, c, d, pts):
    """사다리꼴 점수: a→b 상승, b~c 만점, c→d 하락, 그 밖은 0."""
    if x is None or x <= a or x >= d:
        return 0.0
    if b <= x <= c:
        return float(pts)
    if x < b:
        return pts * (x - a) / (b - a)
    return pts * (d - x) / (d - c)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pick_eligibility(c: Candidate):
    """추천 후보가 될 수 있는지. 반환 (가능 여부, 탈락 사유)."""
    if c.per_loss:
        return False, "적자(PER 없음)"
    if c.op_positive is False:
        return False, "영업적자"
    if not (PICK_MIN_PULLBACK <= c.pullback <= PICK_MAX_PULLBACK):
        return False, f"눌림 {c.pullback:.1f}%가 추천 구간({PICK_MIN_PULLBACK:.0f}~{PICK_MAX_PULLBACK:.0f}%) 밖"
    if not (PICK_MIN_ABOVE_MA <= c.above_ma <= PICK_MAX_ABOVE_MA):
        return False, f"200일선 위 {c.above_ma:.1f}%가 추천 구간 밖"
    if c.ma_slope is not None and c.ma_slope <= 0:
        return False, "200일선이 아직 하락/횡보 중"
    if c.vol_ratio and c.vol_ratio > PICK_MAX_VOL_RATIO:
        return False, f"거래량 {c.vol_ratio:.1f}배 급증"
    if c.flow_foreign is not None and c.flow_orgn is not None and c.flow_foreign < 0 and c.flow_orgn < 0:
        return False, "외국인·기관 동반 순매도"
    return True, ""


def score_candidate(c: Candidate):
    """0~100점과 선정 이유 목록. 배점: 눌림 25 / 추세 20 / 유동성 10 / 거래량 10 / 밸류 15 / 재무 10 / 수급 10."""
    s = 0.0
    why = []

    s += _trap(c.pullback, 5, 10, 15, 20, 25)
    why.append(f"20일 고점 대비 -{c.pullback:.1f}% 조정")

    trend = _trap(c.above_ma, 3, 8, 30, 60, 16)
    slope_pts = _clamp(c.ma_slope * 2, 0, 4) if c.ma_slope is not None else 2.0
    s += trend + slope_pts
    txt = f"200일선 위 +{c.above_ma:.1f}%"
    if c.ma_slope is not None and c.ma_slope > 0:
        txt += " · 200일선 상승 중"
    why.append(txt)

    if c.trade_value > 0:
        s += _clamp(10 * math.log10(max(c.trade_value, 1e-9) / 30.0), 0, 10)

    s += _trap(c.vol_ratio, 0.0, 0.5, 1.5, 3.0, 10)

    rel = c.per_rel if c.per_rel is not None else c.pbr_rel
    if rel is not None:
        s += _clamp(15 * (70 - rel) / 100, 0, 15)
        why.append(f"업종 {'PER' if c.per_rel is not None else 'PBR'} 대비 {rel:+.0f}%")
    else:
        s += 7.0

    fund_known = c.op_positive is not None or c.debt_ratio is not None
    if fund_known:
        f = 0.0
        if (c.op_yoy is not None and c.op_yoy > 0) or c.op_turnaround:
            f += 3
            why.append(f"영업이익 YoY {fmt_yoy(c.op_yoy, c.op_turnaround)}")
        if c.rev_yoy is not None and c.rev_yoy > 0:
            f += 3
        if c.is_financial:
            f += 2
        elif c.debt_ratio is not None:
            f += 4 if c.debt_ratio <= 100 else 2 if c.debt_ratio <= 200 else 1 if c.debt_ratio <= 300 else 0
        s += f
    else:
        s += 5.0

    if c.flow_foreign is not None and c.flow_orgn is not None:
        pos = (c.flow_foreign > 0) + (c.flow_orgn > 0)
        s += {2: 10.0, 1: 6.0, 0: 3.0}[pos]
        if pos == 2:
            why.append("외국인·기관 5일 동반 순매수")
        elif c.flow_foreign > 0:
            why.append("외국인 5일 순매수")
        elif c.flow_orgn > 0:
            why.append("기관 5일 순매수")
    else:
        s += 5.0

    return round(s, 1), why


def _select_diverse(sorted_cands, n):
    """점수순 목록에서 n개 고르되, 가능하면 업종이 겹치지 않게."""
    picked, used = [], set()
    for c in sorted_cands:
        if len(picked) >= n:
            break
        key = c.sector or c.code
        if key in used:
            continue
        picked.append(c)
        used.add(key)
    for c in sorted_cands:
        if len(picked) >= n:
            break
        if all(c is not p for p in picked):
            picked.append(c)
    return picked


def pick_top(cands, n: int = PICK_N, flow_fn=None, log_cb=None):
    """조건 충족 종목 중 추천 후보 n개. 하드 필터 통과 → 점수 → (상위권만) 수급 조회 → 재점수 → 업종 분산."""
    def log(msg):
        if log_cb:
            log_cb(msg)

    pool = []
    for c in cands:
        ok, _why = pick_eligibility(c)
        if ok:
            c.score, c.reasons = score_candidate(c)
            pool.append(c)
    pool.sort(key=lambda c: c.score, reverse=True)

    if flow_fn and pool:
        shortlist = pool[:max(n * 4, 10)]
        rest = pool[len(shortlist):]
        for c in shortlist:
            flow_fn(c)
        survivors = []
        for c in shortlist:
            ok, why = pick_eligibility(c)   # 수급 데이터가 붙은 뒤 다시 판정
            if ok:
                c.score, c.reasons = score_candidate(c)
                survivors.append(c)
            else:
                log(f"  [추천 제외] {c.name}: {why}")
        pool = survivors + rest
        pool.sort(key=lambda c: c.score, reverse=True)

    return _select_diverse(pool, n)


# ═════════════════════════════════════════════════════════════════
#  직전 스캔 대비 신규/이탈
# ═════════════════════════════════════════════════════════════════
def _load_snapshot():
    try:
        return json.loads(_SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_snapshot(hits):
    try:
        _SNAPSHOT_FILE.write_text(
            json.dumps(
                {"as_of": _now_kst().strftime("%Y-%m-%d %H:%M"), "hits": {c.code: c.name for c in hits}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════
#  스캔 본체
# ═════════════════════════════════════════════════════════════════
def run_scan(
    top_n: int = TOP_N,
    min_mcap_억: int = MIN_MCAP_억,
    pullback_pct: float = PULLBACK_PCT,
    lookback: int = LOOKBACK,
    ma_period: int = MA_PERIOD,
    min_avg_trade_value_억: float = MIN_AVG_TRADE_VALUE_억,
    max_per: float = MAX_PER,
    max_pbr: float = MAX_PBR,
    use_dart: bool = USE_DART,
    max_debt_ratio: float = MAX_DEBT_RATIO,
    require_op_profit: bool = REQUIRE_OP_PROFIT,
    max_pullback_pct: float = MAX_PULLBACK_PCT,
    require_ma_rising: bool = REQUIRE_MA_RISING,
    use_flow: bool = USE_FLOW,
    pick_n: int = PICK_N,
    progress_cb=None,
    stop_flag=None,
    log_cb=None,
) -> ScanResult:
    """
    스캔을 실행하고 ScanResult 를 반환한다.

    progress_cb(done, total, code, name) : 종목 1개 처리할 때마다 호출 (GUI 진행바용)
    stop_flag() -> bool                  : True 를 반환하면 중단
    log_cb(str)                          : 로그 메시지 전달

    유동성: 최근 lookback일 평균거래대금이 min_avg_trade_value_억 미만이면 제외.
    장중 실행: 오늘의 미완성 봉은 거래량비/거래대금/이동평균에서 제외(고점에는 포함).
    상장 기간이 ma_period 미만인 종목은 이동평균을 믿을 수 없어 제외.
    밸류에이션: max_per / max_pbr 이 0보다 크면 초과 종목 제외. 적자(PER 없음)는 거르지 않고 '적자'로 표시.
    DART: 부채비율 상한·영업적자 제외. 금융업은 부채비율 필터 면제. 데이터 없는 종목은 통과.
          키 오류·한도 초과 시 한 번만 알리고 이후 DART 조회를 중단한다.
    추천(picks): 조건 충족(hits) 중 pick_eligibility 를 통과한 종목을 score_candidate 로 점수화,
                 상위권은 외국인/기관 수급(use_flow)을 확인한 뒤 업종이 겹치지 않게 pick_n 개.
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    params = dict(
        top_n=top_n, min_mcap_억=min_mcap_억, pullback_pct=pullback_pct, max_pullback_pct=max_pullback_pct,
        lookback=lookback, ma_period=ma_period, min_avg_trade_value_억=min_avg_trade_value_억,
        max_per=max_per, max_pbr=max_pbr, use_dart=use_dart, max_debt_ratio=max_debt_ratio,
        require_op_profit=require_op_profit, require_ma_rising=require_ma_rising, use_flow=use_flow,
    )

    kf._load_keys()
    if use_dart:
        try:
            dart._load_key()
        except Exception as e:
            log(f"[DART] 키 설정 문제로 재무 조건을 건너뜁니다: {e}")
            use_dart = False

    now = _now_kst()
    today = now.strftime("%Y%m%d")
    intraday = is_intraday(now)
    if intraday:
        log("[참고] 장중 실행 — 오늘 봉은 미완성이라 거래량비·거래대금·이동평균은 직전 거래일까지 기준입니다.")

    log(f"KRX 시총 상위 종목 로딩 중... (상위 {top_n}개, {min_mcap_억:,}억 이상)")
    universe = load_top_marcap(top_n, min_mcap_억, log_cb=log)
    log(f"대상 대형주: {len(universe)}개")

    hits, knives = [], []
    scanned = skipped_short = skipped_risk = 0
    sector_stats: dict = {}
    total = len(universe)
    stopped = False
    dart_dead = False

    for idx, (code, name, mcap) in enumerate(universe, start=1):
        if stop_flag and stop_flag():
            log("사용자 중단 요청 — 스캔을 멈춥니다.")
            stopped = True
            break

        daily = None
        try:
            daily = _fetch_history(code, ma_period + 30, lookback=lookback)
        except Exception as e:
            log(f"  [경고] {name}({code}) 시세 조회 실패: {e}")

        if progress_cb:
            progress_cb(idx, total, code, name)

        m = compute_metrics(daily, lookback, ma_period, intraday, today)
        if not m:
            continue
        scanned += 1

        if m["avg_trade_value"] < min_avg_trade_value_억:
            continue  # 유동성 부족 — 매매가 뜸한 종목은 제외

        rt = None
        try:
            rt = kf.fetch_realtime(code)
        except Exception as e:
            log(f"  [경고] {name}({code}) 현재가 조회 실패: {e}")
        price = (rt or {}).get("price") or m["last_close"]
        per = (rt or {}).get("per")
        pbr = (rt or {}).get("pbr")
        per_loss = bool((rt or {}).get("per_loss"))
        sector = (rt or {}).get("sector") or ""

        risk = (rt or {}).get("risk") or []
        if risk:
            skipped_risk += 1
            log(f"  [제외] {name}({code}): {', '.join(risk)}")
            continue

        # 업종 중앙값 계산용 표본 (눌림 조건과 무관하게 유동성 통과 종목 전체)
        if sector:
            st = sector_stats.setdefault(sector, {"per": [], "pbr": []})
            if per:
                st["per"].append(per)
            if pbr:
                st["pbr"].append(pbr)

        if not m["ma_ok"]:
            skipped_short += 1
            continue  # 상장 기간이 짧아 200일선을 계산할 수 없음

        if max_per and per is not None and per > max_per:
            continue
        if max_pbr and pbr is not None and pbr > max_pbr:
            continue

        high_n = m["high_n"]
        pullback = (high_n - price) / high_n * 100 if high_n else 0.0
        ma_val = m["ma_val"]
        uptrend = price > ma_val
        above_ma = (price - ma_val) / ma_val * 100 if ma_val else 0.0

        if pullback < pullback_pct:
            continue
        if max_pullback_pct and pullback > max_pullback_pct:
            continue
        if uptrend and require_ma_rising and m["ma_slope"] is not None and m["ma_slope"] <= 0:
            continue

        c = Candidate(
            name=name, code=code, mcap=mcap, price=price, pullback=pullback, above_ma=above_ma,
            trade_value=m["avg_trade_value"], vol_ratio=m["vol_ratio"], ma_slope=m["ma_slope"],
            sector=sector, per=per, pbr=pbr, per_loss=per_loss, partial_day=m["partial"],
        )
        c.is_financial = is_financial(sector, None, name)

        if use_dart and not dart_dead:
            fin = None
            try:
                fin = dart.fetch_financials(code, log_cb=log)
            except dart.DartFatalError as e:
                dart_dead = True
                log(f"[DART] {e} — 이번 스캔에서는 DART 재무 조건을 더 이상 쓰지 않습니다.")
            except Exception as e:
                log(f"  [DART 경고] {name}({code}) 재무조회 실패: {e}")
            if fin:
                c.debt_ratio = fin.get("debt_ratio")
                c.op_yoy = fin.get("op_profit_yoy")
                c.rev_yoy = fin.get("revenue_yoy")
                c.op_positive = fin.get("op_profit_positive")
                c.op_turnaround = bool(fin.get("op_turnaround"))
                c.fin_period = fin.get("period") or ""
                if not sector:
                    c.is_financial = is_financial("", c.debt_ratio, name)

            if (max_debt_ratio and c.debt_ratio is not None and c.debt_ratio > max_debt_ratio
                    and not c.is_financial):
                continue
            if require_op_profit and c.op_positive is False:
                continue

        (hits if uptrend else knives).append(c)

    # 업종 중앙값 대비 밸류에이션
    medians = {}
    for sec, st in sector_stats.items():
        medians[sec] = {
            k: (statistics.median(v) if len(v) >= 3 else None) for k, v in st.items()
        }
    for c in hits + knives:
        md = medians.get(c.sector)
        if md:
            if c.per and md.get("per"):
                c.per_rel = (c.per / md["per"] - 1) * 100
            if c.pbr and md.get("pbr"):
                c.pbr_rel = (c.pbr / md["pbr"] - 1) * 100

    # 추천 3종목
    def flow_fn(c):
        try:
            f = kf.fetch_investor_flow(c.code, 5)
        except Exception as e:
            log(f"  [수급 경고] {c.name}({c.code}) 조회 실패: {e}")
            return
        if f:
            c.flow_foreign, c.flow_orgn, c.flow_unit = f["foreign"], f["orgn"], f["unit"]

    picks = []
    if not stopped:
        picks = pick_top(hits, n=pick_n, flow_fn=flow_fn if use_flow else None, log_cb=log)

    # 점수 미산정(추천 자격 밖) 종목도 표에서 순서가 일관되도록 점수는 추천 대상만 갖고, 나머지는 눌림순
    hits.sort(key=lambda c: (c.score is None, -(c.score or 0), -c.pullback))
    knives.sort(key=lambda c: c.pullback, reverse=True)

    # 직전 스캔 대비
    exited = []
    prev = _load_snapshot()
    if prev and not stopped:
        prev_hits = prev.get("hits") or {}
        for c in hits:
            c.is_new = c.code not in prev_hits
        cur_codes = {c.code for c in hits}
        exited = [n for code, n in prev_hits.items() if code not in cur_codes]
        if exited:
            log(f"[변동] 직전 스캔({prev.get('as_of', '?')}) 대비 이탈: {', '.join(exited[:10])}")
    if not stopped:
        _save_snapshot(hits)

    log(
        f"스캔 완료: {scanned}개 조회 / 충족 {len(hits)}개 / 칼날주의 {len(knives)}개 / 추천 {len(picks)}개"
        + (f" / 상장기간 부족 제외 {skipped_short}개" if skipped_short else "")
        + (f" / 위험 플래그 제외 {skipped_risk}개" if skipped_risk else "")
    )
    return ScanResult(
        hits=hits, knives=knives, picks=picks, scanned=scanned, universe_len=len(universe),
        skipped_short_history=skipped_short, skipped_risk=skipped_risk, intraday=intraday,
        stopped=stopped, as_of=now.strftime("%Y-%m-%d %H:%M"), exited=exited, params=params,
    )


def main():
    print("=" * 78)
    print(f"  대장주 자동 발굴  [{_now_kst().strftime('%Y-%m-%d %H:%M')}]")
    print(f"  조건: 시총상위 {TOP_N}개(≥{MIN_MCAP_억:,}억) + 200일선 위 + {LOOKBACK}일고점 대비 -{PULLBACK_PCT:.0f}% "
          f"+ 평균거래대금 ≥{MIN_AVG_TRADE_VALUE_억}억")
    print("=" * 78)

    try:
        kf._load_keys()
    except Exception as e:
        print(f"[!] KIS 키가 없어 실행할 수 없습니다: {e}")
        return

    try:
        res = run_scan(log_cb=print)
    except ImportError:
        print("[!] FinanceDataReader 없음. 설치:  pip install -r requirements.txt")
        return
    except Exception as e:
        print(f"[!] 스캔 실패: {e}")
        return

    print(f"\n[i] 스캔 완료: {res.scanned}개 조회 (대상 {res.universe_len}개)")

    print("=" * 78)
    print(f"  ⭐ 추천 후보 {len(res.picks)}종목 (조건 점수 기준 — 매수 권유 아님)")
    print("=" * 78)
    if res.picks:
        for i, c in enumerate(res.picks, 1):
            print(f"  {i}. {c.name} ({c.code})  [{c.sector or '업종 미확인'}]  점수 {c.score:.0f}  현재가 {c.price:,}")
            for r in c.reasons:
                print(f"       - {r}")
    else:
        print("  추천 기준(눌림 7~20%, 200일선 상승, 거래량 과열 아님, 흑자 등)을 모두 통과한 종목이 없습니다.")

    print("\n" + "=" * 78)
    print(f"  🟢 조건 충족 — 대형주 + 상승추세(200일선 위) + 눌림 ({len(res.hits)}개)")
    print("=" * 78)
    if res.hits:
        print(f"  {'종목명':14} {'코드':>7} {'업종':10} {'현재가':>9} {'눌림':>6} {'200선위':>7} {'거래대금':>8} "
              f"{'거래량비':>6} {'PER':>6} {'PBR':>5} {'업종PER':>7}")
        print("  " + "-" * 104)
        for c in res.hits[:MAX_SHOW]:
            pbr_s = f"{c.pbr:.2f}" if c.pbr is not None else "-"
            rel_s = f"{c.per_rel:+.0f}%" if c.per_rel is not None else "-"
            print(f"  {c.name:14} {c.code:>7} {c.sector[:8]:10} {c.price:>9,} {c.pullback:>5.1f}% {c.above_ma:>6.1f}% "
                  f"{c.trade_value:>7,.0f}억 {c.vol_ratio:>5.1f}x {fmt_per(c):>6} {pbr_s:>5} {rel_s:>7}")
        print("\n  ※ '눌림' 클수록 고점서 많이 빠짐 / '200일선위' 클수록 추세 강함.")
        print("  ※ 조건 스크리닝일 뿐. 매수 여부·종목·금액은 본인 판단.")
    else:
        print("  조건 충족 종목 없음 (지금 대형주 대부분 200일선 아래거나 눌림 부족).")

    if res.knives:
        print("\n" + "-" * 78)
        print(f"  ⚠️ 참고: 눌림은 왔지만 하락추세(200일선 아래) — '칼날' 주의 ({len(res.knives)}개)")
        print("  " + ", ".join(k.name for k in res.knives[:15]))
    print("=" * 78)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print("\n[오류 발생]")
        traceback.print_exc()
    print()
    input("=== 끝났습니다. 엔터 키를 누르면 창이 닫힙니다. ===")
