# -*- coding: utf-8 -*-
"""
dart_feed.py — DART(전자공시시스템) Open API 최소 래퍼.

auto_finder.py 가 재무 펀더멘털(부채비율, 영업이익 흑자여부, 전년동기대비
증감률)을 추가로 확인할 때 쓴다. KIS는 시세만 주고 재무제표는 안 주기
때문에 별도로 둔다.

키 설정 방법 (우선순위):
  1) set_key(api_key, save=True) 로 직접 지정 (GUI에서 사용)
  2) 환경변수 DART_API_KEY
  3) 같은 폴더의 dart_key.json

준비물: opendart.fss.or.kr 에서 무료로 발급받은 인증키
"""
from __future__ import annotations

import json
import os
import time
import zipfile
import io
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

_BASE = "https://opendart.fss.or.kr/api"
_KEY_FILE = Path(__file__).with_name("dart_key.json")
_CORPCODE_CACHE = Path(__file__).with_name(".dart_corpcode_cache.json")
_FIN_CACHE = Path(__file__).with_name(".dart_financials_cache.json")

_CORPCODE_TTL = 30 * 24 * 3600     # 종목-corp_code 매핑은 한 달에 한 번이면 충분
_FIN_TTL = 7 * 24 * 3600           # 재무제표는 분기마다만 바뀌므로 일주일 캐시

_state = {"api_key": None}
_last_call = {"t": 0.0}
_MIN_INTERVAL = 0.08


def _throttle():
    now = time.time()
    wait = _MIN_INTERVAL - (now - _last_call["t"])
    if wait > 0:
        time.sleep(wait)
    _last_call["t"] = time.time()


def _read_key_file():
    if _KEY_FILE.exists():
        try:
            return json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def set_key(api_key: str, save: bool = False):
    _state["api_key"] = api_key
    if save:
        _KEY_FILE.write_text(json.dumps({"api_key": api_key}, ensure_ascii=False), encoding="utf-8")


def _load_key(api_key: str | None = None):
    if api_key:
        _state["api_key"] = api_key
        return
    if _state["api_key"]:
        return
    env_key = os.environ.get("DART_API_KEY")
    if env_key:
        _state["api_key"] = env_key
        return
    cfg = _read_key_file()
    if cfg.get("api_key"):
        _state["api_key"] = cfg["api_key"]
        return
    raise RuntimeError(
        "DART API 키가 설정되지 않았습니다. GUI에서 입력하거나 dart_key.json / "
        "환경변수(DART_API_KEY)를 설정하세요. (무료 발급: opendart.fss.or.kr)"
    )


def _get(path: str, params: dict, retries: int = 3):
    if not _state["api_key"]:
        _load_key()
    full_params = {"crtfc_key": _state["api_key"], **params}

    last_exc = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(f"{_BASE}/{path}", params=full_params, timeout=10)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(0.5 * (attempt + 1))
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return None


def _load_corpcode_cache():
    if _CORPCODE_CACHE.exists():
        try:
            data = json.loads(_CORPCODE_CACHE.read_text(encoding="utf-8"))
            if time.time() - data.get("fetched_at", 0) < _CORPCODE_TTL:
                return data.get("map")
        except Exception:
            pass
    return None


def _download_corpcode_map(log_cb=None):
    """DART 전체 상장사의 (종목코드6자리 -> corp_code8자리) 매핑을 받아온다."""
    resp = _get("corpCode.xml", params={})
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)

    mapping = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code and corp_code and len(stock_code) == 6:
            mapping[stock_code] = corp_code

    _CORPCODE_CACHE.write_text(
        json.dumps({"fetched_at": time.time(), "map": mapping}, ensure_ascii=False),
        encoding="utf-8",
    )
    if log_cb:
        log_cb(f"[DART] 상장사 코드 매핑 {len(mapping)}건 갱신 완료")
    return mapping


def get_corp_code(stock_code6: str, log_cb=None):
    """KRX 종목코드(6자리) -> DART corp_code(8자리). 없으면 None."""
    mapping = _load_corpcode_cache()
    if mapping is None:
        mapping = _download_corpcode_map(log_cb=log_cb)
    return mapping.get(stock_code6)


_REPORT_CANDIDATES_BY_YEAR_OFFSET = {
    0: ["11014", "11012", "11013"],   # 올해: 3분기 → 반기 → 1분기 순으로 최신 것부터
    1: ["11011", "11014", "11012", "11013"],  # 작년: 사업보고서(연간) 우선
    2: ["11011"],
}

_ACCOUNT_ALIASES = {
    "assets": ["자산총계"],
    "liabilities": ["부채총계"],
    "equity": ["자본총계"],
    "revenue": ["매출액", "수익(매출액)"],
    "operating_profit": ["영업이익", "영업이익(손실)"],
    "net_profit": ["당기순이익", "당기순이익(손실)"],
}


def _load_fin_cache():
    if _FIN_CACHE.exists():
        try:
            return json.loads(_FIN_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_fin_cache(cache: dict):
    try:
        _FIN_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _parse_amount(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _extract_accounts(rows):
    """단일회사 주요계정 API 응답 list에서 필요한 계정만 뽑는다.
    연결재무제표(CFS)가 있으면 우선, 없으면 별도재무제표(OFS)를 쓴다."""
    by_fs = {"CFS": {}, "OFS": {}}
    for row in rows:
        fs_div = row.get("fs_div")
        if fs_div not in by_fs:
            continue
        name = (row.get("account_nm") or "").strip()
        for key, aliases in _ACCOUNT_ALIASES.items():
            if name in aliases and key not in by_fs[fs_div]:
                by_fs[fs_div][key] = {
                    "thstrm": _parse_amount(row.get("thstrm_amount")),
                    "frmtrm": _parse_amount(row.get("frmtrm_amount")),
                }

    chosen = by_fs["CFS"] if len(by_fs["CFS"]) >= len(by_fs["OFS"]) else by_fs["OFS"]
    return chosen if chosen else None


def _compute_metrics(accounts, year, reprt_code):
    def thstrm(key):
        v = accounts.get(key)
        return v["thstrm"] if v else None

    def frmtrm(key):
        v = accounts.get(key)
        return v["frmtrm"] if v else None

    assets = thstrm("assets")
    liabilities = thstrm("liabilities")
    equity = thstrm("equity")
    revenue_t, revenue_f = thstrm("revenue"), frmtrm("revenue")
    op_t, op_f = thstrm("operating_profit"), frmtrm("operating_profit")
    net_t = thstrm("net_profit")

    debt_ratio = (liabilities / equity * 100) if (liabilities is not None and equity) else None
    revenue_yoy = ((revenue_t - revenue_f) / abs(revenue_f) * 100) if (revenue_t is not None and revenue_f) else None
    op_profit_yoy = ((op_t - op_f) / abs(op_f) * 100) if (op_t is not None and op_f) else None
    op_profit_positive = (op_t > 0) if op_t is not None else None
    net_profit_positive = (net_t > 0) if net_t is not None else None

    return {
        "debt_ratio": debt_ratio,
        "revenue_yoy": revenue_yoy,
        "op_profit_yoy": op_profit_yoy,
        "op_profit_positive": op_profit_positive,
        "net_profit_positive": net_profit_positive,
        "period": f"{year} {reprt_code}",
    }


def fetch_financials(stock_code6: str, log_cb=None):
    """
    종목의 최신 재무 펀더멘털을 반환한다.
    반환: {debt_ratio, revenue_yoy, op_profit_yoy, op_profit_positive,
           net_profit_positive, period} 또는 None(조회 불가)
    모든 값은 개별적으로 None일 수 있다(그 계정이 공시에 없는 경우).
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    corp_code = get_corp_code(stock_code6, log_cb=log_cb)
    if not corp_code:
        return None

    cache = _load_fin_cache()
    cached = cache.get(corp_code)
    if cached and time.time() - cached.get("fetched_at", 0) < _FIN_TTL:
        return cached.get("metrics")

    import datetime
    this_year = datetime.datetime.now().year

    for year_offset, reprt_codes in _REPORT_CANDIDATES_BY_YEAR_OFFSET.items():
        year = this_year - year_offset
        for reprt_code in reprt_codes:
            try:
                resp = _get(
                    "fnlttSinglAcnt.json",
                    params={"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code},
                )
                j = resp.json()
            except Exception as e:
                log(f"[DART] {stock_code6} 재무조회 실패({year}/{reprt_code}): {e}")
                continue

            if j.get("status") != "000":
                continue  # 해당 분기 보고서 아직 미제출 등 — 다음 후보로

            accounts = _extract_accounts(j.get("list") or [])
            if not accounts:
                continue

            metrics = _compute_metrics(accounts, year, reprt_code)
            cache[corp_code] = {"fetched_at": time.time(), "metrics": metrics}
            _save_fin_cache(cache)
            return metrics

    # 아무 후보에서도 데이터를 못 찾음 — 조회 불가로 캐시(짧게)해서 반복 재시도 방지
    cache[corp_code] = {"fetched_at": time.time(), "metrics": None}
    _save_fin_cache(cache)
    return None
