# -*- coding: utf-8 -*-
"""
kis_feed.py — 한국투자증권(KIS) Open API 최소 래퍼.

auto_finder.py 가 재사용하는 함수만 제공한다:
  _load_keys()          : 키 로딩 + 접근토큰 발급/캐시
  _get(...)             : 인증된 GET 요청 (자동 재시도, 토큰 만료 시 재발급 후 1회 재시도)
  _throttle()           : 요청 간 최소 간격 보장 (레이트리밋 방지)
  _i / _f               : 문자열 → int / float 안전 변환 ("1,234" 같은 콤마 포함 허용)
  fetch_realtime(code6) : 현재가 + PER/PBR + 업종명 + 위험 플래그(거래정지/시장경고 등)
  fetch_investor_flow() : 외국인/기관 최근 N일 순매수

키 설정 방법 (우선순위):
  1) set_keys(...) 또는 _load_keys(app_key=..., app_secret=..., is_virtual=...) 로 직접 지정 (GUI)
  2) 환경변수 KIS_APP_KEY / KIS_APP_SECRET / KIS_IS_VIRTUAL
  3) 같은 폴더의 kis_keys.json  (kis_keys.example.json 참고)
이미 1)로 지정된 키가 있으면 _load_keys() 가 파일/환경변수로 덮어쓰지 않는다.
"""
from __future__ import annotations

import base64
import json
import os
import time
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

_BASE_REAL = "https://openapi.koreainvestment.com:9443"
_BASE_VIRTUAL = "https://openapivts.koreainvestment.com:29443"

_KEY_FILE = Path(__file__).with_name("kis_keys.json")
_TOKEN_FILE = Path(__file__).with_name(".kis_token_cache.json")

_KST = timezone(timedelta(hours=9))

_state = {
    "app_key": None,
    "app_secret": None,
    "is_virtual": False,
    "base_url": _BASE_REAL,
    "access_token": None,
    "token_expire": 0.0,
}

_lock = threading.Lock()
_last_call = {"t": 0.0}
_MIN_INTERVAL_REAL = 0.06      # 실전투자: 초당 약 15~16회 (KIS 레이트리밋 여유있게)
_MIN_INTERVAL_VIRTUAL = 0.55   # 모의투자: 실전보다 훨씬 엄격(초당 ~2회 수준) — 여유있게 1.8회/초

_TOKEN_MARGIN = 60             # 만료 60초 전부터는 새 토큰으로 취급
_TOKEN_ERROR_CODES = {"EGW00123", "EGW00121"}   # 기간이 만료된 token / 유효하지 않은 token
_RATE_LIMIT_CODE = "EGW00201"                   # 초당 거래건수 초과


def _i(v, default=None):
    """문자열/숫자를 정수로 안전 변환. 콤마·공백 제거, 실패 시 default."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return int(v)
        except Exception:
            return default
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "None"):
        return default
    try:
        return int(float(s))
    except Exception:
        return default


def _f(v, default=None):
    """문자열/숫자를 실수(float)로 안전 변환."""
    if v is None:
        return default
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "None"):
        return default
    try:
        return float(s)
    except Exception:
        return default


def _throttle():
    with _lock:
        min_interval = _MIN_INTERVAL_VIRTUAL if _state["is_virtual"] else _MIN_INTERVAL_REAL
        now = time.time()
        wait = min_interval - (now - _last_call["t"])
        if wait > 0:
            time.sleep(wait)
        _last_call["t"] = time.time()


def set_keys(app_key: str, app_secret: str, is_virtual: bool = False, save: bool = False):
    """GUI 등에서 직접 키를 지정할 때 사용. save=True면 kis_keys.json 에 저장."""
    _state["app_key"] = app_key
    _state["app_secret"] = app_secret
    _state["is_virtual"] = bool(is_virtual)
    _state["base_url"] = _BASE_VIRTUAL if is_virtual else _BASE_REAL
    _state["access_token"] = None
    _state["token_expire"] = 0.0
    if save:
        _KEY_FILE.write_text(
            json.dumps(
                {"app_key": app_key, "app_secret": app_secret, "is_virtual": bool(is_virtual)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _read_key_file():
    if _KEY_FILE.exists():
        try:
            return json.loads(_KEY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ── 토큰 만료 시각 계산 ────────────────────────────────────────
def _jwt_exp(token):
    """JWT payload 의 exp(초 단위 epoch). 실패 시 None."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


def _parse_kis_dt(s):
    """'2026-10-28 09:00:00' (KST) → epoch. 실패 시 None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_KST).timestamp()
    except Exception:
        return None


def _load_token_cache():
    """캐시된 토큰이 유효하면 (access_token, expire) 튜플을, 아니면 None을 반환."""
    if not _TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("app_key") != _state["app_key"]:
            return None
        if data.get("base_url", _BASE_REAL) != _state["base_url"]:
            return None  # 실전/모의 토큰 혼용 방지
        token = data.get("access_token")
        expire = float(data.get("expire", 0))
        jwt_exp = _jwt_exp(token) if token else None
        if jwt_exp:
            expire = min(expire, jwt_exp)   # 예전 캐시에 기록된 부정확한 만료시각 보정
        if token and expire > time.time() + _TOKEN_MARGIN:
            return token, expire
    except Exception:
        pass
    return None


def _save_token_cache():
    try:
        _TOKEN_FILE.write_text(
            json.dumps(
                {
                    "app_key": _state["app_key"],
                    "base_url": _state["base_url"],
                    "access_token": _state["access_token"],
                    "expire": _state["token_expire"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


_AUTH_FAIL_COOLDOWN = 60  # 토큰 발급 실패 시 이 시간(초) 동안은 재요청 없이 바로 에러
_auth_fail = {"until": 0.0, "message": ""}


def _invalidate_token():
    """현재 토큰과 캐시 파일을 버린다(만료/무효 응답을 받았을 때)."""
    _state["access_token"] = None
    _state["token_expire"] = 0.0
    _auth_fail["until"] = 0.0
    try:
        _TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _issue_token():
    # 최근에 발급이 실패했으면 쿨다운 동안은 서버에 다시 요청하지 않고 바로 실패
    # (실패한 채로 계속 재요청하면 레이트리밋에 더 걸리거나 일시 차단될 수 있음)
    now = time.time()
    if now < _auth_fail["until"]:
        wait = int(_auth_fail["until"] - now)
        raise RuntimeError(
            f"{_auth_fail['message']} (최근 발급 실패로 {wait}초간 재시도를 쉽니다)"
        )

    url = _state["base_url"] + "/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": _state["app_key"],
        "appsecret": _state["app_secret"],
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        msg = f"토큰 발급 요청 실패(네트워크): {e}"
        _auth_fail["until"] = time.time() + _AUTH_FAIL_COOLDOWN
        _auth_fail["message"] = msg
        raise RuntimeError(msg) from e

    if resp.status_code != 200:
        # KIS는 실패 사유를 JSON 본문(msg_cd/msg1)으로 알려주는 경우가 많아 최대한 노출한다.
        try:
            body = resp.json()
            detail = body.get("msg1") or body.get("error_description") or str(body)
        except Exception:
            detail = resp.text[:300]
        msg = f"토큰 발급 실패 (HTTP {resp.status_code}): {detail}"
        _auth_fail["until"] = time.time() + _AUTH_FAIL_COOLDOWN
        _auth_fail["message"] = msg
        raise RuntimeError(msg)

    j = resp.json()
    token = j.get("access_token")
    if not token:
        msg = f"토큰 발급 실패: {j}"
        _auth_fail["until"] = time.time() + _AUTH_FAIL_COOLDOWN
        _auth_fail["message"] = msg
        raise RuntimeError(msg)

    # 만료 시각: 서버가 알려주는 값들 중 가장 이른 것을 쓴다(실제 만료보다 늦게 잡히는 문제 방지).
    expire = time.time() + _i(j.get("expires_in"), 86400)
    for cand in (_jwt_exp(token), _parse_kis_dt(j.get("access_token_token_expired"))):
        if cand:
            expire = min(expire, cand)

    _state["access_token"] = token
    _state["token_expire"] = expire
    _auth_fail["until"] = 0.0
    _save_token_cache()
    return token


def _load_keys(app_key: str | None = None, app_secret: str | None = None, is_virtual: bool | None = None):
    """키를 로딩하고 접근토큰을 준비한다.
    인자를 주면 그 값을 우선, 이미 set_keys()로 지정된 값이 있으면 그것을 유지한다(GUI 입력값 보호)."""
    if app_key and app_secret:
        set_keys(app_key, app_secret, bool(is_virtual))
    elif _state["app_key"] and _state["app_secret"]:
        pass  # GUI 등에서 이미 지정됨 — 파일/환경변수로 덮어쓰지 않는다
    else:
        env_key = os.environ.get("KIS_APP_KEY")
        env_secret = os.environ.get("KIS_APP_SECRET")
        if env_key and env_secret:
            set_keys(env_key, env_secret, os.environ.get("KIS_IS_VIRTUAL", "0") == "1")
        else:
            cfg = _read_key_file()
            if cfg.get("app_key") and cfg.get("app_secret"):
                set_keys(cfg["app_key"], cfg["app_secret"], bool(cfg.get("is_virtual", False)))

    if not _state["app_key"] or not _state["app_secret"]:
        raise RuntimeError(
            "KIS APP KEY / SECRET 이 설정되지 않았습니다. "
            "GUI에서 입력하거나 kis_keys.json / 환경변수(KIS_APP_KEY, KIS_APP_SECRET)를 설정하세요."
        )

    if _state["access_token"] and time.time() < _state["token_expire"] - _TOKEN_MARGIN:
        return

    cached = _load_token_cache()
    if cached:
        _state["access_token"], _state["token_expire"] = cached
        return

    _throttle()
    _issue_token()


def _ensure_token():
    if not _state["access_token"] or time.time() > _state["token_expire"] - _TOKEN_MARGIN:
        _throttle()
        _issue_token()


def _headers(tr_id: str):
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_state['access_token']}",
        "appkey": _state["app_key"],
        "appsecret": _state["app_secret"],
        "tr_id": tr_id,
        "custtype": "P",
    }


def _get(path: str, tr_id: str, params: dict, retries: int = 3):
    """인증된 GET 요청.
    - 429/5xx/네트워크 오류는 재시도
    - 토큰 만료·무효 응답은 토큰을 재발급해 1회 재시도
    - 초당 호출 제한(EGW00201)은 잠시 쉬고 재시도"""
    if not _state["app_key"]:
        _load_keys()
    _ensure_token()

    url = _state["base_url"] + path
    last_exc = None
    refreshed = False

    for attempt in range(retries + 1):
        _throttle()
        try:
            resp = requests.get(url, headers=_headers(tr_id), params=params, timeout=10)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(0.5 * (attempt + 1))
            continue

        body = None
        try:
            body = resp.json()
        except ValueError:
            pass
        msg_cd = (body or {}).get("msg_cd")

        if msg_cd in _TOKEN_ERROR_CODES and not refreshed:
            refreshed = True
            _invalidate_token()
            _ensure_token()
            continue
        if msg_cd == _RATE_LIMIT_CODE:
            time.sleep(1.0)
            continue

        if resp.status_code == 200:
            # KIS는 실패해도 HTTP 200을 주고 본문의 rt_cd로 성패를 알리는 경우가 흔하다.
            rt_cd = (body or {}).get("rt_cd")
            if rt_cd is not None and rt_cd != "0":
                raise RuntimeError(
                    f"KIS API 오류(tr_id={tr_id}, rt_cd={rt_cd}): {(body or {}).get('msg1', '')}"
                )
            return body
        if resp.status_code in (429, 500, 502, 503):
            time.sleep(0.5 * (attempt + 1))
            continue
        resp.raise_for_status()

    if last_exc:
        raise last_exc
    raise RuntimeError(f"KIS 응답을 받지 못했습니다(tr_id={tr_id}, 재시도 초과)")


def _yn(v, default="N"):
    return str(v if v not in (None, "") else default).strip().upper()


def fetch_realtime(code6: str):
    """
    현재가 조회. 같은 API 응답에 PER/PBR/업종/위험 플래그도 같이 오므로 추가 호출 없이 반환.
    반환: {"price": int, "per": float|None, "pbr": float|None, "per_loss": bool,
           "sector": str, "risk": [str, ...]} 또는 None
    - PER이 0 이하(적자)면 per=None, per_loss=True 로 돌려준다.
    - risk: 거래정지/시장경고(02·03)/관리종목/정리매매 해당 시 사유 문자열 목록.
    """
    j = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        tr_id="FHKST01010100",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6},
    )
    out = (j or {}).get("output") or {}
    price = _i(out.get("stck_prpr"))
    if price is None:
        return None

    per = _f(out.get("per"))
    pbr = _f(out.get("pbr"))
    eps = _f(out.get("eps"))
    per_loss = (per is not None and per <= 0) or (per is None and eps is not None and eps <= 0)
    if per is not None and per <= 0:
        per = None
    if pbr is not None and pbr <= 0:
        pbr = None

    risk = []
    if _yn(out.get("temp_stop_yn")) == "Y":
        risk.append("거래정지")
    if str(out.get("mrkt_warn_cls_code") or "00").strip() in ("02", "03"):
        risk.append("시장경고")
    if _yn(out.get("mang_issu_cls_code")) == "Y":
        risk.append("관리종목")
    if _yn(out.get("sltr_yn")) == "Y":
        risk.append("정리매매")

    # 아래는 scalper.py(초단타 모니터링)용 — 같은 응답에서 추가 호출 없이 뽑는다.
    # ⚠️ shnu_cntg_qty/seln_cntg_qty 필드명은 실제 KIS 응답으로 검증된 적 없음(HANDOFF 참고).
    buy_qty = _i(out.get("shnu_cntg_qty"))
    sell_qty = _i(out.get("seln_cntg_qty"))
    cttr = (buy_qty / sell_qty * 100) if (buy_qty is not None and sell_qty) else None

    sign = _i(out.get("prdy_vrss_sign"), 0)  # 1,2=상승 4,5=하락 3=보합
    change = _i(out.get("prdy_vrss"))
    if change is not None and sign in (4, 5) and change > 0:
        change = -change

    return {
        "price": price,
        "per": per,
        "pbr": pbr,
        "per_loss": per_loss,
        "sector": (out.get("bstp_kor_isnm") or "").strip(),
        "risk": risk,
        "change": change,                      # 전일대비(부호 있음)
        "change_pct": _f(out.get("prdy_ctrt")),  # 등락률(%)
        "volume": _i(out.get("acml_vol")),     # 누적거래량
        "buy_cntg_qty": buy_qty,               # 누적 매수체결량
        "sell_cntg_qty": sell_qty,             # 누적 매도체결량
        "cttr": cttr,                          # 체결강도(%) — 100 넘으면 매수우위
    }


def fetch_investor_flow(code6: str, days: int = 5):
    """
    최근 days거래일 외국인/기관 순매수 합계.
    반환: {"foreign": float, "orgn": float, "unit": "억"|"주", "days": int} 또는 None
    거래대금(백만원) 필드가 있으면 억원 단위, 없으면 수량(주) 단위로 합산한다.
    장중에는 당일 행이 비어 있을 수 있어 값이 있는 행만 쓴다.
    """
    j = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        tr_id="FHKST01010900",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6},
    )
    rows = (j or {}).get("output") or []
    usable = []
    for r in rows:
        fq, oq = _f(r.get("frgn_ntby_qty")), _f(r.get("orgn_ntby_qty"))
        if fq is None or oq is None:
            continue
        usable.append((fq, oq, _f(r.get("frgn_ntby_tr_pbmn")), _f(r.get("orgn_ntby_tr_pbmn"))))
        if len(usable) >= days:
            break
    if not usable:
        return None

    if all(u[2] is not None and u[3] is not None for u in usable):
        return {
            "foreign": sum(u[2] for u in usable) / 100.0,   # 백만원 → 억원
            "orgn": sum(u[3] for u in usable) / 100.0,
            "unit": "억",
            "days": len(usable),
        }
    return {
        "foreign": sum(u[0] for u in usable),
        "orgn": sum(u[1] for u in usable),
        "unit": "주",
        "days": len(usable),
    }


def fetch_orderbook(code6: str):
    """
    실시간 호가(매수/매도 1~10단계 잔량) 조회 — scalper.py용.
    ⚠️ askp/askp_rsqn/bidp/bidp_rsqn/total_*_rsqn 필드명은 실제 KIS 응답으로 검증된 적 없음.
    반환: {
      "ask1": int, "bid1": int,                    # 매도1호가/매수1호가
      "total_ask_qty": int, "total_bid_qty": int,  # 총 매도/매수 잔량
      "bid_ask_ratio": float|None,                 # 총매수잔량/총매도잔량 (1보다 크면 매수우위)
      "levels": [(ask_price, ask_qty, bid_price, bid_qty), ...]  # 1~10단계
    } 또는 None
    """
    j = _get(
        "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        tr_id="FHKST01010200",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6},
    )
    out = (j or {}).get("output1") or {}
    if not out:
        return None

    levels = []
    for n in range(1, 11):
        levels.append((
            _i(out.get(f"askp{n}")), _i(out.get(f"askp_rsqn{n}")),
            _i(out.get(f"bidp{n}")), _i(out.get(f"bidp_rsqn{n}")),
        ))

    total_ask = _i(out.get("total_askp_rsqn"))
    total_bid = _i(out.get("total_bidp_rsqn"))
    ratio = (total_bid / total_ask) if total_ask else None

    return {
        "ask1": _i(out.get("askp1")),
        "bid1": _i(out.get("bidp1")),
        "total_ask_qty": total_ask,
        "total_bid_qty": total_bid,
        "bid_ask_ratio": ratio,
        "levels": levels,
    }
