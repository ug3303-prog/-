# -*- coding: utf-8 -*-
"""
kis_feed.py — 한국투자증권(KIS) Open API 최소 래퍼.

auto_finder.py 가 재사용하는 함수만 제공한다:
  _load_keys()      : 키 로딩 + 접근토큰 발급/캐시
  _get(...)         : 인증된 GET 요청 (자동 재시도 포함)
  _throttle()       : 요청 간 최소 간격 보장 (레이트리밋 방지)
  _i(v, default)    : 문자열 → int 안전 변환 ("1,234" 같은 콤마 포함 허용)
  fetch_realtime(code6) : 현재가 조회

키 설정 방법 (우선순위):
  1) _load_keys(app_key=..., app_secret=..., is_virtual=...) 로 직접 전달 (GUI에서 사용)
  2) 환경변수 KIS_APP_KEY / KIS_APP_SECRET / KIS_IS_VIRTUAL
  3) 같은 폴더의 kis_keys.json  (kis_keys.example.json 참고)
"""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path

import requests

_BASE_REAL = "https://openapi.koreainvestment.com:9443"
_BASE_VIRTUAL = "https://openapivts.koreainvestment.com:29443"

_KEY_FILE = Path(__file__).with_name("kis_keys.json")
_TOKEN_FILE = Path(__file__).with_name(".kis_token_cache.json")

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
_MIN_INTERVAL = 0.06  # 초당 약 15~16회로 제한 (KIS 레이트리밋 여유있게)


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


def _throttle():
    with _lock:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _last_call["t"])
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


def _load_token_cache():
    if _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            if data.get("app_key") == _state["app_key"] and data.get("expire", 0) > time.time() + 30:
                return data.get("access_token")
        except Exception:
            pass
    return None


def _save_token_cache():
    try:
        _TOKEN_FILE.write_text(
            json.dumps(
                {
                    "app_key": _state["app_key"],
                    "access_token": _state["access_token"],
                    "expire": _state["token_expire"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _issue_token():
    url = _state["base_url"] + "/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": _state["app_key"],
        "appsecret": _state["app_secret"],
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    j = resp.json()
    token = j.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 발급 실패: {j}")
    expires_in = _i(j.get("expires_in"), 86400)
    _state["access_token"] = token
    _state["token_expire"] = time.time() + expires_in
    _save_token_cache()
    return token


def _load_keys(app_key: str | None = None, app_secret: str | None = None, is_virtual: bool | None = None):
    """키를 로딩하고 접근토큰을 준비한다. 인자를 주면 그 값을 우선 사용."""
    if app_key and app_secret:
        set_keys(app_key, app_secret, bool(is_virtual))
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

    cached = _load_token_cache()
    if cached:
        _state["access_token"] = cached
        return

    _throttle()
    _issue_token()


def _ensure_token():
    if not _state["access_token"] or time.time() > _state["token_expire"] - 30:
        _throttle()
        _issue_token()


def _get(path: str, tr_id: str, params: dict, retries: int = 3):
    """인증된 GET 요청. 429/일시 오류는 재시도."""
    if not _state["app_key"]:
        _load_keys()
    _ensure_token()

    url = _state["base_url"] + path
    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {_state['access_token']}",
        "appkey": _state["app_key"],
        "appsecret": _state["app_secret"],
        "tr_id": tr_id,
        "custtype": "P",
    }

    last_exc = None
    for attempt in range(retries):
        _throttle()
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
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


def fetch_realtime(code6: str):
    """
    현재가 조회. 같은 API 응답에 PER/PBR도 같이 오므로 추가 호출 없이 반환.
    반환: {"price": int, "per": float|None, "pbr": float|None} 또는 None
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
    return {
        "price": price,
        "per": _f(out.get("per")),
        "pbr": _f(out.get("pbr")),
    }
