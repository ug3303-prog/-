# -*- coding: utf-8 -*-
"""
단타매수 스캐너 v17 — v14 기반 · 한국투자증권(KIS) API 내장판
════════════════════════════════════════════════════════════

[버전 이력 요약]
  v11  당일 실시간 가격/거래량 기반 스코어링, 갭업·눌림·과열 페널티
  v12  거래량 U자곡선 환산 · 시장 레짐 게이트 · 당일급등 과열 · 갭업 정합성
  v13  자금관리 · 오늘의 추천 포트폴리오
  v14  매매일지 자동 누적 · 성과 리포트 · KIS 실시간 피더 · 시총 필터
  v15  하락 종목 오추천 수정 (휴림로봇 사례)
       · KIS 내장(kis_feed.py 불필요) · 네이버 폴백 제거 · 이상값 채점 거부
       · 차단 게이트: 거래정지/투자경고·위험/관리 · 유증·CB·BW 공시(DART) ·
         20·60일선 하락 추세 · 거래대금 부족 · 갭업 3%↑ · 당일 15%↑
       · '저점 근처'·'D-2 대비 하락' 가점 → '상승 추세 속 눌림 + 반등 확인'으로 교체
       · 거래량 실린 음봉 감점 · VWAP · 체결강도 · 기관/외국인 3일 · 섹터 동조
       · 비우호 레짐에서 모멘텀 모드가 켜지던 버그 수정
       · --review: 매매일지 추천을 KIS 일봉으로 자동 채점 (익절/손절 먼저)
  v16  인수인계 검증 반영: 슬리피지 0.6% · 지수 ETF 20일선 게이트 · ATR 손절/목표(호가단위) ·
       위험 기준 수량(종목당 자본 1%) · 영문 혼합 신규 종목코드(0015G0형) 지원 · 스캔 대상 KIS 자동 수집
  v16.1 선도매수 남발 수정 (10/2 스캔 28개 → 정원제)
       · 페널티가 등급 상한이 됨: 당일 7%↑·5일 20%↑·연속양봉 4일↑·체결강도 100 미만·프로그램 대량 순매도·
         윗꼬리·섹터 약세·기관외인 매도·VWAP 아래·음봉·거래량 1.5배 미만이면 선도 불가(매수대기도 '관찰')
       · 선도매수 정원 = 보유 종목 수 × 2 (점수 상위만)
       · 레짐을 시장 전체 상승/하락 종목 수로 판정 (스캔 대상은 거래 상위라 상승 쪽으로 편향)

  v17  2026-11 매매일지 32건 복기 반영 (플러스 5건 / 90점↑ 1/10 / 11:30 이후 0/8)
       · 급등 말기 차단: 20일선 이격 20%↑ · 20일 저점 대비 +40%↑ / 이격 12%↑는 감점 + 진입 등급 불가
       · 하루 변동폭(ATR)이 손절 상한보다 큰 종목은 진입 등급 불가 (손절이 잡음에 걸림)
       · 지수 ETF 둘 다 20일선 아래 또는 당일 -1.5%↓ → 신규 진입 중단 (임계값 상향만으로는 부족했음)
       · 진입 시간대(개장 30분·점심 소강·장외 제외)에만 포트폴리오·매매일지 기록
       · 섹터당 1종목 + 20일 수익률 상관 0.7↑ 종목 중복 편입 금지 (같은 테마 몰빵 방지)
       · 품질 조건에 걸리면 '관심'까지만 → 화면의 선도매수·매수대기는 전부 조건 통과 종목
       · 프로그램·기관 수급을 주식 수가 아닌 금액 기준으로 / 개장 직후 거래량 가속 과대평가 완화
       · 자동 채점에 추천 당일 종가 반영 + 점수대·시간대별 성적 / 조회 실패를 하루 종일 캐시하던 버그 수정

사용법 (v14와 동일 + 신규 3개):
  python danta_v16.py                     ← 1회 실행 + 추천 포트폴리오
  python danta_v16.py --watch 5           ← 5분마다 반복 (장중 자동)
  python danta_v16.py --alert             ← 선도 진입 신호 실시간 모니터
  python danta_v16.py --holding 휴림로봇:6500   ← 보유 종목 손익 + 위험 신호
  python danta_v16.py --capital 10000000 --positions 5 --stop 5 --profit 8
  python danta_v16.py --report            ← [매매일지] 수기 결과 집계 (v14)
  python danta_v16.py --check             ← [v15] KIS 연결 + 종목풀 코드 점검
  python danta_v16.py --review            ← [v15] 추천 자동 채점 → 승률·기대값
  python danta_v16.py --selftest          ← [v15] API 없이 채점 로직 테스트

필요 파일: kis_config.json (app_key, app_secret, mode, dart_api_key)
"""

import sys, os, time, argparse, json, io, zipfile, math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment

# ══════════════════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════════════════
SCRIPT_DIR    = Path(__file__).parent
REQUEST_DELAY = 0             # v15: KIS 클라이언트가 호출 간격을 직접 관리


# ── v11 스코어 가중치 (합계 MAX 100점) ──
W = {
    "volume_accel":   30,   # ①장중 거래량 가속도 (선행) — 비중 높임
    "pre_signal":     25,   # ②저점 위치 + 갭업 없음    — 핵심 필터
    "pullback":       20,   # ③눌림목 반등
    "candle_seq":     10,   # ④연속 양봉
    "inst_fore":      15,   # ⑤기관+외국인 수급 (신규)
    "hot_penalty":   -25,   # ⑥과열 페널티 (유지)
}

# 판정 임계값
GRADE = {"선도매수": 60, "매수대기": 45, "관심": 28}

# ── v12.2 당일 급등 과열 임계값 (%) ────────────────────────────────
# 5일 등락률만으로는 '오늘 하루 20% 급등'을 못 잡는다. 당일 상승폭 자체를
# 별도 페널티로 본다. BLOCK 이상은 점수와 무관하게 진입 후보에서 제외.
HOT_INTRADAY_CAUTION = 7.0    # 진입가 불리 (-7점)
HOT_INTRADAY_WARN    = 10.0   # 고점 추격 위험 (-15점)
HOT_INTRADAY_BLOCK   = 15.0   # 추격 금지 — 등급 강제 제외

# ── v14+ 시가총액 필터 (억원) ──────────────────────────────────────
# 단타 전략상 시총 1,000~3,000억 중소형이 핵심(상한가 전조 원칙 1).
# KIS 현재가 응답의 hts_avls(시총)로 매 실행마다 실시간 필터링한다.
# 범위 밖이면 등급을 '⬇제외'로 강등(대형주·초소형주 자동 배제).
# 완전 차단이 부담되면 넓게 잡거나, MCAP_FILTER_ON=False로 끌 수 있다.
MCAP_FILTER_ON  = os.environ.get("MCAP_FILTER", "1") != "0"
MCAP_MIN = 2000     # 하한 (억원) — 초소형 배제로 널뛰기 변동성 억제
MCAP_MAX = 10000    # 상한 (억원) — 대장주 동조/낙수효과까지 포착

# ── v15 차단 게이트 설정 ──────────────────────────────────────────────
MIN_AVG_AMOUNT_EOK = 20       # 20일 평균 거래대금 하한 (억원)
DART_LOOKBACK_DAYS = 30       # 악재 공시 확인 기간 (일)
REVIEW_HORIZON     = 5        # --review 자동 검증 기간 (거래일)
# KIS 종목상태/시장경고 코드 중 진입 차단 대상
BLOCK_STAT_CODES = {"51": "관리종목", "52": "투자위험", "53": "투자경고", "58": "거래정지", "59": "단기과열"}
BLOCK_WARN_CODES = {"02": "투자경고", "03": "투자위험"}

# 시간대별 전략
TIME_STRATEGY = [
    ((9, 0),  (9, 30),  "❌진입금지",   "변동성 극대 / 세력 물량 투하"),
    ((9, 30), (10, 30), "🚀선도진입",   "거래량 가속+저점 종목만 / 기관 동반 필수"),
    ((10,30), (11, 30), "⬆눌림포착",   "눌림목 반등 + 전일종가 돌파"),
    ((11,30), (14, 10), "⏸소강구간",   "신규 진입 자제"),
    ((14,10), (14, 40), "✅최적단타",   "오후 눌림 종료 / 기관 수급 방향 확정"),
    ((14,40), (15, 30), "📈마감전",     "매수대기 이상 + 시간외 고가 종목만"),
]

# ══════════════════════════════════════════════════════════════════
# v13 자금관리 · 포지션 추천 기본값 (CLI --capital/--positions/--stop/--profit 로 변경)
# ══════════════════════════════════════════════════════════════════
DEFAULT_CAPITAL    = 9_000_000    # 총 투입 자금 (원) — 단타 슬리브 900만
DEFAULT_MAX_POS    = 3            # 동시 보유 목표 종목 수
DEFAULT_STOP_PCT   = 5.0          # 손절 기준 (%)
DEFAULT_PROFIT_PCT = 8.0          # 익절 기준 (%)
MAX_PER_SECTOR     = 1            # v17: 섹터당 1종목 (매매일지: 반도체장비 한 테마에 6종목 몰려 동반 손절)
ROUNDTRIP_COST_PCT = 0.25         # 왕복 매매비용 추정치(%) = 2026년 거래세 0.20% + 수수료 감안
# v16: 슬리피지(체결 미끄러짐) 별도 반영 — 급등주·호가 얇은 종목은 현재가에 못 산다
SLIPPAGE_ROUNDTRIP_PCT = 0.6
TOTAL_FRICTION_PCT = ROUNDTRIP_COST_PCT + SLIPPAGE_ROUNDTRIP_PCT
# v16: ATR 기반 손절/익절 + 위험 기준 수량
ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5      # 손절 폭 = 1.5 × ATR
MIN_STOP_PCT    = 2.0      # 손절 폭 하한 (%) — 너무 촘촘하면 잡음에 털림
REWARD_RISK     = 2.0      # 목표 폭 = 손절 폭 × 2  (손익비 2:1)
RISK_PER_TRADE_PCT = 1.0   # 종목당 최대 손실 = 자본의 1%
DAILY_LOSS_LIMIT_PCT = 3.0 # 전 종목 동시 손절 시 자본 대비 경고선
# v16: 시장 지수 게이트 (지수 ETF 20일선) — 스캔 유니버스 breadth와 별개로 시장 전체 위험 반영
MARKET_INDEX_ETF = [("코스피(KODEX200)", "069500"), ("코스닥(KODEX코스닥150)", "229200")]
MAX_EXTRA_THRESHOLD = 25   # 레짐 + 지수 게이트 합산 상향 폭 상한 (이중 반영 방지)

# ── v17 (2026-11 매매일지 32건 복기 반영) ─────────────────────────────
# 추천 32건 중 플러스 5건 / 점수 90점↑이 오히려 더 나쁨(1/10) / 11:30 이후 추천 0/8 /
# 기계·장비+전기·전자(같은 반도체장비 테마) 18건 중 플러스 1건.
# → '많이 오른 종목일수록 고득점'이던 구조를 고치고, 시장·시간·분산 조건을 강제한다.
DISPARITY_WARN     = 12.0   # 20일선 이격 12%↑: 감점 + 선도 불가 (이미 달린 종목)
DISPARITY_BLOCK    = 20.0   # 20일선 이격 20%↑: 차단 (평균 회귀 위험)
RUNUP20_BLOCK      = 40.0   # 20거래일 저점 대비 +40%↑: 차단 (급등 말기)
INDEX_DAY_BLOCK    = -1.5   # 지수 ETF 당일 -1.5%↓: 신규 진입 중단
BLOCK_ENTRY_ON_INDEX_DOWN = True   # 코스피·코스닥 ETF 둘 다 20일선 아래면 신규 진입 중단
STOP_CAP_PCT       = DEFAULT_STOP_PCT   # 손절 상한(%) — GUI가 덮어씀. ATR이 이보다 크면 손절이 잡음에 걸린다
CORR_LIMIT         = 0.70   # 포트폴리오 내 20일 일간수익률 상관 상한 (같은 테마 중복 방지)
PROG_BIG_EOK       = 5.0    # 프로그램 '대량' 기준: 순매수 금액 5억원 (주식 수 기준은 주가에 따라 왜곡)

# ── v12 거래량 누적 분포 곡선 ──────────────────────────────────────
# 한국 정규장(09:00~15:30, 390분) 거래량은 균등하지 않고 U자로 분포한다.
# 개장 직후 1시간·마감 동시호가에 집중되고 점심에 급감한다.
# (경과분, 그 시각까지 쌓이는 하루 거래량의 누적 비율) — 일반적 패턴 근사치.
# ※ 종목/시장마다 다르므로, 여유 되면 본인 워치리스트의 분 단위 거래량을
#   며칠 누적해 실측 곡선으로 교체하면 정확도가 더 올라간다.
VOLUME_PROFILE = [
    (0,   0.000),   # 09:00 개장
    (30,  0.180),   # 09:30  (v17: 중소형 급등주는 개장 30분에 더 몰린다 → 가속 과대평가 완화)
    (60,  0.290),   # 10:00
    (120, 0.380),   # 11:00
    (180, 0.480),   # 12:00
    (240, 0.560),   # 13:00 — 점심 부근 정체
    (300, 0.680),   # 14:00
    (360, 0.820),   # 15:00
    (380, 0.900),   # 15:20 종가 동시호가 직전
    (390, 1.000),   # 15:30 마감
]

# ══════════════════════════════════════════════════════════════════
# 유틸
# ══════════════════════════════════════════════════════════════════
def to_int(s) -> Optional[int]:
    if s is None: return None
    try: return int(str(s).replace(",","").replace("+","").strip())
    except: return None

def to_float(s) -> Optional[float]:
    if s is None: return None
    try: return float(str(s).replace(",","").replace("+","").strip())
    except: return None

_NOW_OVERRIDE = None          # --selftest 용 시각 고정

def _now() -> datetime:
    return _NOW_OVERRIDE or datetime.now()


def market_status() -> str:
    now = _now()
    if now.weekday() >= 5: return "AFTER"
    t = now.hour * 100 + now.minute
    if t < 900:   return "BEFORE"
    if t <= 1530: return "OPEN"
    return "AFTER"

def current_time_strategy() -> tuple:
    now = _now()
    h, m = now.hour, now.minute
    for (sh,sm),(eh,em), label, desc in TIME_STRATEGY:
        if (h*60+m) >= (sh*60+sm) and (h*60+m) < (eh*60+em):
            return label, desc
    return "장외", "장 외 시간"

def elapsed_market_minutes() -> float:
    """장 시작(09:00) 이후 경과 분 (최소 1분)"""
    now = _now()
    open_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
    elapsed = max(1, (now - open_time).total_seconds() / 60)
    return min(elapsed, 390)  # 최대 6.5시간


def expected_volume_fraction(elapsed_min: float) -> float:
    """
    장 시작 후 경과분 기준, '정상적이라면 하루 거래량의 몇 %가 쌓였을지'를
    VOLUME_PROFILE에서 선형보간해 반환 (0.0~1.0).

    예) 경과 30분 → 약 0.13. 즉 30분간 거래량은 하루치의 13%가 정상.
        선형 환산(390/30 = ×13)은 이 시각 거래량을 ×13으로 부풀리지만,
        곡선 환산은 ÷0.13(= ×7.7)이라 과대평가가 줄어든다.
    """
    if elapsed_min <= 0:
        return 0.001
    if elapsed_min >= 390:
        return 1.0
    for i in range(1, len(VOLUME_PROFILE)):
        m0, f0 = VOLUME_PROFILE[i - 1]
        m1, f1 = VOLUME_PROFILE[i]
        if elapsed_min <= m1:
            span = (m1 - m0) or 1
            return f0 + (f1 - f0) * (elapsed_min - m0) / span
    return 1.0

# ══════════════════════════════════════════════════════════════════
# 데이터 수집 — v15: 한국투자증권(KIS) Open API 내장
# ══════════════════════════════════════════════════════════════════
# v14는 kis_feed.py가 없거나 실패하면 '조용히' 네이버 스크래핑으로 넘어갔다.
# 네이버 HTML 정규식은 자주 엉뚱한 숫자를 잡아 등락률 0 · 갭업 -99%~+2500% ·
# 레인지위치 -14 같은 불가능한 값이 점수에 섞였다(실제 엑셀에서 확인된 버그).
# → v15는 KIS 단일 소스. 실패한 종목은 '가짜 값으로 채점'하지 않고 건너뛴다.
CONFIG_PATH = SCRIPT_DIR / "kis_config.json"
TOKEN_PATH  = SCRIPT_DIR / ".kis_token.json"
CACHE_DIR   = SCRIPT_DIR / "cache_v16"

CONFIG_TEMPLATE = {
    "app_key": "여기에_APP_KEY",
    "app_secret": "여기에_APP_SECRET",
    "mode": "real",
    "dart_api_key": "",
}


class KISError(Exception):
    pass


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(CONFIG_TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
        raise KISError(f"{CONFIG_PATH.name} 을 새로 만들었습니다. 메모장으로 열어 app_key/app_secret 을 넣으세요.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("app_key") or "여기에" in str(cfg.get("app_key")):
        raise KISError(f"{CONFIG_PATH.name} 에 app_key/app_secret 이 비어 있습니다.")
    return cfg


class KIS:
    REAL = "https://openapi.koreainvestment.com:9443"
    MOCK = "https://openapivts.koreainvestment.com:29443"

    def __init__(self, app_key: str, app_secret: str, mode: str = "real"):
        self.app_key, self.app_secret = app_key.strip(), app_secret.strip()
        self.mock = str(mode).lower().startswith("mock")
        self.base = self.MOCK if self.mock else self.REAL
        self.min_interval = 0.55 if self.mock else 0.07     # 실전 초당 20건 / 모의 2건 한도 이하
        self._last = 0.0
        self.calls = 0
        self.s = requests.Session()
        self.token = self._get_token()

    def _get_token(self) -> str:
        # 토큰은 24시간 유효, 발급은 1분 1회 제한 → 파일 캐시
        if TOKEN_PATH.exists():
            try:
                t = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
                if (t.get("app_key") == self.app_key and t.get("base") == self.base
                        and datetime.fromisoformat(t["expires"]) > datetime.now() + timedelta(minutes=10)):
                    return t["token"]
            except Exception:
                pass
        r = self.s.post(f"{self.base}/oauth2/tokenP", json={
            "grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret}, timeout=10)
        j = r.json() if r.content else {}
        tok = j.get("access_token")
        if not tok:
            raise KISError(f"토큰 발급 실패: {j.get('error_description') or j.get('msg1') or r.text[:200]}")
        exp = datetime.now() + timedelta(seconds=int(j.get("expires_in", 86400)) - 60)
        TOKEN_PATH.write_text(json.dumps({"token": tok, "expires": exp.isoformat(),
                                          "app_key": self.app_key, "base": self.base}), encoding="utf-8")
        return tok

    def get(self, path: str, tr_id: str, params: dict, retries: int = 3) -> dict:
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            headers = {"content-type": "application/json; charset=utf-8",
                       "authorization": f"Bearer {self.token}",
                       "appkey": self.app_key, "appsecret": self.app_secret,
                       "tr_id": tr_id, "custtype": "P"}
            try:
                r = self.s.get(f"{self.base}{path}", headers=headers, params=params, timeout=10)
                j = r.json()
            except Exception as e:
                if attempt < retries:
                    time.sleep(1.0); continue
                raise KISError(f"통신 오류: {e}")
            if j.get("rt_cd") == "0":
                return j
            code = j.get("msg_cd", "")
            if code == "EGW00201" and attempt < retries:                 # 초당 호출 초과
                time.sleep(1.0); continue
            if code in ("EGW00123", "EGW00121") and attempt < retries:   # 토큰 만료
                TOKEN_PATH.unlink(missing_ok=True); time.sleep(61)
                self.token = self._get_token(); continue
            raise KISError(f"{tr_id} {code} {j.get('msg1', '')}".strip())
        raise KISError(f"{tr_id} 재시도 초과")


_KIS: Optional[KIS] = None
_DART = None


def init_feeds():
    """KIS(필수) + DART(선택) 연결. 실패하면 예외 → 가짜 데이터로 돌지 않게 즉시 중단."""
    global _KIS, _DART
    if _KIS is not None:
        return
    cfg = load_config()
    _KIS = KIS(cfg["app_key"], cfg["app_secret"], cfg.get("mode", "real"))
    print(f"[피더] KIS {'모의' if _KIS.mock else '실전'} 서버 연결")
    if cfg.get("dart_api_key"):
        try:
            _DART = Dart(cfg["dart_api_key"])
            print(f"[피더] DART 공시 연결 ({len(_DART.map):,}개 기업)")
        except Exception as e:
            print(f"[피더] DART 연결 실패 ({e}) — 공시 차단 없이 진행")
    else:
        print("[피더] DART 키 없음 — 유상증자·CB 공시 차단 꺼짐 (kis_config.json 에 dart_api_key 권장)")


def _f(x, default=None):
    try:
        if x is None or str(x).strip() == "":
            return default
        return float(str(x).replace(",", ""))
    except Exception:
        return default


def _i(x, default=None):
    v = _f(x, None)
    return int(v) if v is not None else default


def norm_code(code) -> Optional[str]:
    """종목코드 6자리 정규화. 2024년 이후 신규 상장의 영문 혼합 코드(예: 0015G0)도 허용."""
    if code is None:
        return None
    c = str(code).strip().upper()
    if c.endswith(".0"):
        c = c[:-2]
    if c.isdigit():
        return c.zfill(6)
    return c if len(c) == 6 and c.isalnum() else None


def tick_size(price: float) -> int:
    """KRX 호가단위 (2023년 개편, 코스피·코스닥 공통)."""
    if price < 2000: return 1
    if price < 5000: return 5
    if price < 20000: return 10
    if price < 50000: return 50
    if price < 200000: return 100
    if price < 500000: return 500
    return 1000


def round_tick(price: float, up: bool) -> int:
    t = tick_size(price)
    return int(math.ceil(price / t) * t) if up else int(math.floor(price / t) * t)


def calc_atr(hist: list, n: int = ATR_PERIOD) -> Optional[float]:
    """hist: 최신 → 과거 확정 일봉. Wilder 방식 대신 단순평균 TR (단타용으로 충분)."""
    if not hist or len(hist) < n + 1:
        return None
    trs = []
    for i in range(n):
        h, l, pc = hist[i]["high"], hist[i]["low"], hist[i + 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def trade_plan(price: float, hist: list, max_stop_pct: float, today_low: Optional[float] = None) -> dict:
    """ATR 기반 손절가·목표가 (호가단위 반영 — NH 자동주문에 그대로 입력)."""
    atr = calc_atr(hist)
    atr_pct = atr / price * 100 if atr and price else None
    stop_pct = ATR_STOP_MULT * atr_pct if atr_pct else max_stop_pct
    stop_pct = max(MIN_STOP_PCT, min(stop_pct, max_stop_pct))
    stop = price * (1 - stop_pct / 100)
    basis = "ATR"
    if today_low and today_low * 0.99 > stop and (price - today_low * 0.99) / price * 100 >= MIN_STOP_PCT:
        stop, basis = today_low * 0.99, "당일저가"                # 구조적 지지(당일 저가) 이탈이 더 가까우면 그쪽
    stop = round_tick(stop, up=False)
    real_stop_pct = (price - stop) / price * 100
    target = round_tick(price * (1 + real_stop_pct * REWARD_RISK / 100), up=True)
    return {"atr": atr, "atr_pct": round(atr_pct, 2) if atr_pct else None, "stop": stop, "target": target,
            "stop_pct": round(real_stop_pct, 2), "target_pct": round((target / price - 1) * 100, 2), "basis": basis}


def fetch_daily(code6: str) -> Optional[list]:
    """최근 일봉 최대 100개 (최신 → 과거). 20·60일선 추세 게이트용으로 v14(25개)보다 길게."""
    end = datetime.now()
    start = end - timedelta(days=150)
    j = _KIS.get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6,
                  "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                  "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    rows = []
    for b in j.get("output2") or []:
        if not b or not b.get("stck_bsop_date"):
            continue
        c = _i(b.get("stck_clpr"), 0)
        if c <= 0:
            continue
        rows.append({"date": b["stck_bsop_date"], "open": _i(b.get("stck_oprc"), c),
                     "high": _i(b.get("stck_hgpr"), c), "low": _i(b.get("stck_lwpr"), c),
                     "close": c, "volume": _i(b.get("acml_vol"), 0),
                     "amount": _i(b.get("acml_tr_pbmn"), 0)})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows if len(rows) >= 6 else None


def fetch_realtime(code6: str) -> Optional[dict]:
    """현재가·시고저·누적거래량·VWAP·시총·프로그램/외국인 순매수·경고코드 (KIS 1회 호출)."""
    o = _KIS.get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6})["output"]
    price = _i(o.get("stck_prpr"))
    if not price:
        return None
    vol, amt = _f(o.get("acml_vol"), 0), _f(o.get("acml_tr_pbmn"), 0)
    return {
        "price": price,
        "change_rate": _f(o.get("prdy_ctrt"), 0.0),
        "today_volume": int(vol),
        "today_open": _i(o.get("stck_oprc")) or None,
        "today_high": _i(o.get("stck_hgpr")) or None,
        "today_low": _i(o.get("stck_lwpr")) or None,
        "prev_close": _i(o.get("stck_sdpr")) or None,
        "vwap": _f(o.get("wghn_avrg_stck_prc")) or (amt / vol if vol else None),
        "_mcap": _i(o.get("hts_avls")),
        "_pgtr_ntby": _i(o.get("pgtr_ntby_qty")),
        "_frgn_ntby": _i(o.get("frgn_ntby_qty")),
        "stat_code": str(o.get("iscd_stat_cls_code") or "").strip(),
        "warn_code": str(o.get("mrkt_warn_cls_code") or "").strip(),
        "temp_stop": str(o.get("temp_stop_yn") or "N").upper() == "Y",
        "invt_caful": str(o.get("invt_caful_yn") or "N").upper() == "Y",
        "short_over": str(o.get("short_over_yn") or "N").upper() == "Y",
        "sector": str(o.get("bstp_kor_isnm") or "").strip() or None,     # KIS 업종명
    }


def fetch_investor(code6: str) -> dict:
    """기관/외국인 순매수 — 직전 확정일 + 최근 3일 합계 (장중 당일 행은 비어 있음)."""
    out = _KIS.get("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6}).get("output") or []
    rows = [r for r in out if str(r.get("orgn_ntby_qty") or "").strip() or str(r.get("frgn_ntby_qty") or "").strip()]
    if not rows:
        return {}
    last = rows[0]
    return {"inst_net": _i(last.get("orgn_ntby_qty")), "fore_net_d1": _i(last.get("frgn_ntby_qty")),
            "inst_3d": sum(_i(r.get("orgn_ntby_qty"), 0) for r in rows[:3]),
            "fore_3d": sum(_i(r.get("frgn_ntby_qty"), 0) for r in rows[:3])}


def fetch_strength(code6: str) -> Optional[float]:
    """당일 체결강도 (100 초과 = 매수 체결 우위)."""
    out = _KIS.get("/uapi/domestic-stock/v1/quotations/inquire-ccnl", "FHKST01010300",
                   {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code6}).get("output") or []
    return _f(out[0].get("tday_rltv")) if out else None


def fetch_name(code6: str) -> str:
    o = _KIS.get("/uapi/domestic-stock/v1/quotations/search-stock-info", "CTPF1002R",
                 {"PRDT_TYPE_CD": "300", "PDNO": code6})["output"]
    return (o.get("prdt_abrv_name") or o.get("prdt_name") or "").strip()


# ── DART 악재 공시 (선택) ─────────────────────────────────────────────
BAD_FILING_KEYWORDS = ["유상증자결정", "전환사채권발행결정", "신주인수권부사채권발행결정", "교환사채권발행결정",
                       "감자결정", "불성실공시법인지정", "관리종목지정", "상장적격성", "매매거래정지", "최대주주변경"]


class Dart:
    def __init__(self, key: str):
        self.key = key.strip()
        CACHE_DIR.mkdir(exist_ok=True)
        p = CACHE_DIR / "dart_corp.json"
        if p.exists() and time.time() - p.stat().st_mtime < 30 * 86400:
            self.map = json.loads(p.read_text(encoding="utf-8"))
        else:
            r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml", params={"crtfc_key": self.key}, timeout=30)
            z = zipfile.ZipFile(io.BytesIO(r.content))
            root = ET.fromstring(z.read(z.namelist()[0]))
            self.map = {(it.findtext("stock_code") or "").strip(): it.findtext("corp_code").strip()
                        for it in root.iter("list") if (it.findtext("stock_code") or "").strip()}
            p.write_text(json.dumps(self.map), encoding="utf-8")

    def bad_filings(self, code6: str, days: int = 30) -> list:
        cc = self.map.get(code6)
        if not cc:
            return []
        end = datetime.now()
        j = requests.get("https://opendart.fss.or.kr/api/list.json", params={
            "crtfc_key": self.key, "corp_code": cc, "bgn_de": (end - timedelta(days=days)).strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "page_count": 100}, timeout=15).json()
        if j.get("status") == "013":            # 조회된 데이터 없음 = 공시 없음
            return []
        if j.get("status") != "000":            # 키 오류·한도 초과 등 — '공시 없음'으로 위장하지 않는다
            raise KISError(f"DART {j.get('status')} {j.get('message', '')}")
        return [f"{it.get('rcept_dt', '')} {it.get('report_nm', '').strip()}" for it in j.get("list", [])
                if any(k in (it.get("report_nm") or "").replace(" ", "") for k in BAD_FILING_KEYWORDS)]


def fetch_filings(code6: str) -> list:
    return _DART.bad_filings(code6, DART_LOOKBACK_DAYS) if _DART else []


# ══════════════════════════════════════════════════════════════════
# v15.1: 스캔 대상 자동 수집 (종목풀 엑셀 불필요)
# ══════════════════════════════════════════════════════════════════
# ① KIS 종목 마스터(코스피·코스닥 전 종목, 하루 1번 다운로드)에서
#    시총 범위 · 보통주 · 거래정지/관리/정리매매/시장경고/SPAC/ETF 제외 · 전일 거래대금으로 1차 선별
# ② 장중이면 KIS 거래대금·거래증가율 순위(코스피/코스닥)를 합쳐 '오늘 돈이 몰리는 종목'을 우선
# ③ 우선순위(오늘 순위 포함 → 전일 거래대금 큰 순)로 최대 N개
MASTER_URLS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}
_KOSPI_W = [2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
            1, 9, 5, 5, 1, 1, 1, 2, 1, 1, 1, 2, 2, 2, 3, 1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1, 1, 9,
            9, 9, 5, 9, 8, 9, 3, 1, 1, 1]
_KOSPI_N = ["그룹코드", "시가총액규모", "업종대", "업종중", "업종소", "제조업", "저유동성", "지배구조", "K200섹터", "K100",
            "K50", "KRX", "ETP", "ELW", "KRX100", "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC",
            "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설", "Non1", "KRX증권", "KRX선박", "KRX보험", "KRX운송",
            "SRI", "기준가", "매매단위", "시간외단위", "거래정지", "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시",
            "우회상장", "락구분", "액면변경", "증자구분", "증거금비율", "신용가능", "신용기간", "전일거래량", "액면가", "상장일자",
            "상장주수", "자본금", "결산월", "공모가", "우선주", "공매도과열", "이상급등", "KRX300", "KOSPI", "매출액",
            "영업이익", "경상이익", "당기순이익", "ROE", "기준년월", "시가총액", "그룹사코드", "신용한도초과", "담보대출", "대주가능"]
_KOSDAQ_W = [2, 1, 4, 4, 4, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 9, 5, 5, 1,
             1, 1, 2, 1, 1, 1, 2, 2, 2, 3, 1, 3, 12, 12, 8, 15, 21, 2, 7, 1, 1, 1, 1, 9, 9, 9, 5, 9, 8, 9,
             3, 1, 1, 1]
_KOSDAQ_N = ["그룹코드", "시가총액규모", "업종대", "업종중", "업종소", "벤처", "저유동성", "KRX", "ETP", "KRX100",
             "KRX자동차", "KRX반도체", "KRX바이오", "KRX은행", "SPAC", "KRX에너지화학", "KRX철강", "단기과열", "KRX미디어통신", "KRX건설",
             "투자주의환기", "KRX증권", "KRX선박", "KRX보험", "KRX운송", "KQ150", "기준가", "매매단위", "시간외단위", "거래정지",
             "정리매매", "관리종목", "시장경고", "경고예고", "불성실공시", "우회상장", "락구분", "액면변경", "증자구분", "증거금비율",
             "신용가능", "신용기간", "전일거래량", "액면가", "상장일자", "상장주수", "자본금", "결산월", "공모가", "우선주",
             "공매도과열", "이상급등", "KRX300", "매출액", "영업이익", "경상이익", "당기순이익", "ROE", "기준년월", "시가총액",
             "그룹사코드", "신용한도초과", "담보대출", "대주가능"]


def parse_master_line(line: str, market: str) -> Optional[dict]:
    """KIS 종목 마스터 한 줄 → dict (한국투자증권 공식 샘플과 같은 고정폭 규칙)."""
    widths, names = (_KOSPI_W, _KOSPI_N) if market == "KOSPI" else (_KOSDAQ_W, _KOSDAQ_N)
    line = line.rstrip("\r\n")
    n2 = sum(widths)
    if len(line) <= n2 + 21:
        return None
    p1, p2 = line[:-n2], line[-n2:]
    rec = {"code": p1[0:9].strip(), "name": p1[21:].strip(), "market": market}
    pos = 0
    for w, nm in zip(widths, names):
        rec[nm] = p2[pos:pos + w].strip()
        pos += w
    return rec


def _num(x):
    try:
        return float(str(x).strip() or 0)
    except ValueError:
        return 0.0


def load_master(log=print) -> list:
    """코스피+코스닥 전 종목 마스터 (하루 1번 다운로드 후 캐시)."""
    CACHE_DIR.mkdir(exist_ok=True)
    cp = CACHE_DIR / f"master_{datetime.now():%Y%m%d}.json"
    if cp.exists():
        return json.loads(cp.read_text(encoding="utf-8"))
    rows = []
    for mk, url in MASTER_URLS.items():
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        text = z.read(z.namelist()[0]).decode("cp949", errors="replace")
        n = 0
        for line in text.splitlines():
            rec = parse_master_line(line, mk)
            if rec and rec["code"]:
                rows.append(rec); n += 1
        log(f"  종목 마스터 {mk}: {n:,}개\n")
    for old in CACHE_DIR.glob("master_*.json"):
        old.unlink(missing_ok=True)
    cp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def master_filter(rows: list, mcap_min: float, mcap_max: float, min_amount_eok: float) -> list:
    """보통주·정상 거래 종목 중 시총·전일 거래대금 조건 통과분."""
    out = []
    for r in rows:
        code = r.get("code", "")
        if not norm_code(code) or len(code) != 6:
            continue                                         # 6자리 아닌 코드(ETN 등) 제외, 0015G0형 신규코드는 허용
        if r.get("그룹코드") != "ST":                        # 주권만 (ETF=EF, 리츠 등 제외)
            continue
        if r.get("SPAC") == "Y" or r.get("거래정지") == "Y" or r.get("정리매매") == "Y" or r.get("관리종목") == "Y":
            continue
        if r.get("시장경고", "00") not in ("", "0", "00"):   # 투자주의·경고·위험
            continue
        if r.get("우선주", "0") not in ("", "0"):
            continue
        mcap = _num(r.get("시가총액"))
        if mcap_min > 0 and mcap < mcap_min:
            continue
        if mcap_max > 0 and mcap > mcap_max:
            continue
        amt = _num(r.get("기준가")) * _num(r.get("전일거래량")) / 1e8
        if amt < min_amount_eok * 0.5:                      # 하루치라 느슨하게 (정밀 판정은 20일 평균으로)
            continue
        out.append({"code": code, "name": r["name"], "market": r["market"], "mcap": mcap,
                    "prev_amount": round(amt, 1), "sector": None, "rank_hit": False})
    return out


def fetch_rank(market_iscd: str, blng: str) -> list:
    """KIS 거래량 순위 (최대 30개). blng: 3=거래금액순, 1=거래증가율."""
    j = _KIS.get("/uapi/domestic-stock/v1/quotations/volume-rank", "FHPST01710000", {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_COND_SCR_DIV_CODE": "20171", "FID_INPUT_ISCD": market_iscd,
        "FID_DIV_CLS_CODE": "1", "FID_BLNG_CLS_CODE": blng, "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "1111111101", "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""})
    return [{"code": str(o.get("mksc_shrn_iscd", "")).strip(), "name": str(o.get("hts_kor_isnm", "")).strip()}
            for o in (j.get("output") or []) if o.get("mksc_shrn_iscd")]


def build_universe(mcap_min: float, mcap_max: float, min_amount_eok: float, max_n: int,
                   use_rank: bool = True, log=print) -> list:
    """스캔 대상 종목 목록 [{code,name,market,mcap,prev_amount,sector,rank_hit}] 우선순위순."""
    try:
        master = load_master(log)
    except Exception as e:
        master = []
        log(f"  [!] 종목 마스터 다운로드 실패({e}) — KIS 순위 종목만 사용\n")
    cands = {c["code"]: c for c in master_filter(master, mcap_min, mcap_max, min_amount_eok)}
    by_code = {r["code"]: r for r in master}
    log(f"  조건 통과(시총 {mcap_min:,.0f}~{mcap_max:,.0f}억, 거래대금): {len(cands):,}개\n")

    hits = 0
    if use_rank and market_status() != "BEFORE":
        for mk_iscd in ("0001", "1001"):
            for blng in ("3", "1"):
                try:
                    for r in fetch_rank(mk_iscd, blng):
                        if r["code"] in cands:
                            if not cands[r["code"]]["rank_hit"]:
                                hits += 1
                            cands[r["code"]]["rank_hit"] = True
                        elif not master:                      # 마스터 없을 때만 순위 종목 그대로 사용
                            cands[r["code"]] = {"code": r["code"], "name": r["name"],
                                                "market": "KOSPI" if mk_iscd == "0001" else "KOSDAQ",
                                                "mcap": None, "prev_amount": 0, "sector": None, "rank_hit": True}
                except KISError as e:
                    log(f"  [!] 순위 조회 실패({mk_iscd}/{blng}): {e}\n")
        log(f"  오늘 거래대금·거래증가율 상위와 겹친 종목: {hits}개 (우선 스캔)\n")
    elif not by_code:
        raise KISError("스캔 대상을 만들 수 없음 (마스터 다운로드 실패 + 장 시작 전)")

    uni = sorted(cands.values(), key=lambda c: (not c["rank_hit"], -(c["prev_amount"] or 0)))
    return uni[:max_n] if max_n > 0 else uni



# ── 하루 캐시: 일봉·투자자·공시는 하루 1번만 조회 (--watch 반복 시 API 절약) ──
class DayCache:
    def __init__(self):
        CACHE_DIR.mkdir(exist_ok=True)
        self.path = CACHE_DIR / f"day_{datetime.now():%Y%m%d}.json"
        try:
            self.d = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except Exception:
            self.d = {}
        for old in CACHE_DIR.glob("day_*.json"):
            if old != self.path:
                old.unlink(missing_ok=True)

    def get_or(self, key, fn):
        """v17: fn()이 None(조회 실패)이면 저장하지 않는다 — 한 번 실패한 종목이 하루 종일 빠지던 문제."""
        if self.d.get(key) is None:
            v = fn()
            if v is None:
                return None
            self.d[key] = v
        return self.d[key]

    def save(self):
        self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# v12 핵심 추가: 등급 판정 + 시장 레짐(시장 폭) 게이트
# ══════════════════════════════════════════════════════════════════
def decide_grade(total: float, thresholds: dict) -> str:
    """주어진 임계값 기준으로 등급 결정 (레짐에 따라 임계값이 달라질 수 있음)"""
    if   total >= thresholds["선도매수"]: return "🚀선도매수"
    elif total >= thresholds["매수대기"]: return "⬆매수대기"
    elif total >= thresholds["관심"]:    return "👀관심"
    else:                                 return "⬇제외"


def assess_regime(collected: list, mst: str) -> dict:
    """
    워치리스트 유니버스의 '오늘 시장 폭(breadth)'을 측정해 단타 적합도를 판정한다.
    외부 API 추가 호출 없이, 이미 스캔한 종목들의 등락률/거래량가속으로 계산.

    핵심 발상: 지수가 올라도 내가 칠 풀의 대다수가 빠지는 장이면 단타는 진다.
    상승 종목 비율이 낮을수록 신호 임계값을 끌어올려 '관망'을 유도한다.

    collected: [(daily_row, name, code6, daily, rt, sc), ...]
    반환: {"label","desc","thresholds","adv_ratio","accel_ratio"}
    """
    base = dict(GRADE)  # {"선도매수":60,"매수대기":45,"관심":28}

    if mst != "OPEN":
        return {"label": "⚪판정보류", "desc": "장외 — 실시간 시장 폭 측정 불가, 기본 임계값 사용",
                "thresholds": base, "adv_ratio": None, "accel_ratio": None}

    rates  = [sc.get("change_rate") for *_, sc in collected if sc.get("change_rate") is not None]
    accels = [sc.get("vol_accel", 0) for *_, sc in collected]

    if len(rates) < 5:
        return {"label": "⚪판정보류", "desc": "유효 표본 부족 — 기본 임계값 사용",
                "thresholds": base, "adv_ratio": None, "accel_ratio": None}

    adv_ratio   = sum(1 for r in rates if r > 0) / len(rates)
    accel_ratio = (sum(1 for a in accels if a >= 1.5) / len(accels)) if accels else 0.0

    if adv_ratio >= 0.50:
        label = "🟢단타우호"
        desc  = f"유니버스 상승 {adv_ratio:.0%} · 가속 {accel_ratio:.0%} — 정상 임계값"
        th    = base
    elif adv_ratio >= 0.35:
        label = "🟡중립경계"
        desc  = f"유니버스 상승 {adv_ratio:.0%} — 임계값 +10 보수화"
        th    = {k: v + 10 for k, v in base.items()}
    else:
        label = "🔴단타비우호"
        desc  = f"유니버스 상승 {adv_ratio:.0%} — 관망 권장, 임계값 +20 대폭 상향"
        th    = {k: v + 20 for k, v in base.items()}

    return {"label": label, "desc": desc, "thresholds": th,
            "adv_ratio": adv_ratio, "accel_ratio": accel_ratio}



def assess_market_gate() -> dict:
    """지수 ETF(코스피200·코스닥150)의 20일선 위치로 시장 전체 위험 판정."""
    above, details, checked, day_chgs = 0, [], 0, []
    for label, code in MARKET_INDEX_ETF:
        try:
            daily = fetch_daily(code)
        except Exception:
            daily = None
        if not daily or len(daily) < 20:
            details.append(f"{label}:조회실패"); continue
        checked += 1
        closes = [d["close"] for d in daily]
        up = closes[0] > sum(closes[:20]) / 20
        above += 1 if up else 0
        chg = (closes[0] / closes[1] - 1) * 100
        day_chgs.append(chg)
        details.append(f"{label} {'20일선 위▲' if up else '20일선 아래▼'} {chg:+.1f}%")
    if checked == 0:
        return {"above": None, "extra": 0, "block": False, "label": "⚪지수 확인불가",
                "desc": "지수 조회 실패 — 게이트 미적용"}
    if checked == 1:
        extra, tag = (0, "🟢지수양호") if above else (8, "🟡지수약세")
    elif above == 0:
        extra, tag = 15, "🔴지수하락"
    elif above == 1:
        extra, tag = 8, "🟡지수혼조"
    else:
        extra, tag = 0, "🟢지수양호"
    # v17: 임계값만 올리는 걸로는 부족했다(하락장에서도 고득점 급등주가 통과) → 신규 진입 자체를 멈춘다
    why_block = ""
    if BLOCK_ENTRY_ON_INDEX_DOWN and checked == len(MARKET_INDEX_ETF) and above == 0:
        why_block = "코스피·코스닥 모두 20일선 아래"
    elif day_chgs and min(day_chgs) <= INDEX_DAY_BLOCK:
        why_block = f"지수 당일 {min(day_chgs):+.1f}% 급락"
    desc = f"{tag} (+{extra}) — {' / '.join(details)}"
    if why_block:
        desc += f"  ⛔ 신규 진입 중단 ({why_block})"
    return {"above": above, "extra": extra, "label": tag, "desc": desc,
            "block": bool(why_block), "block_reason": why_block}


def daily_returns(hist: list, n: int = 20) -> list:
    """최근 n거래일 일간 수익률(%) — 포트폴리오 상관 계산용. hist: 최신 → 과거."""
    c = [d["close"] for d in hist[:n + 1]]
    return [round((c[i] / c[i + 1] - 1) * 100, 2) for i in range(len(c) - 1) if c[i + 1]]


def correlation(a, b) -> Optional[float]:
    if not a or not b:
        return None
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[:n], b[:n]
    ma_, mb_ = sum(a) / n, sum(b) / n
    va = sum((x - ma_) ** 2 for x in a); vb = sum((y - mb_) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma_) * (y - mb_) for x, y in zip(a, b)) / math.sqrt(va * vb)


def pick_diversified(cands: list, max_pos: int) -> list:
    """점수순 후보에서 섹터당 MAX_PER_SECTOR개 + 서로 상관 CORR_LIMIT 미만인 종목만 고른다.
    KIS 업종명은 같은 테마(예: 반도체 장비)를 '기계·장비'와 '전기·전자'로 갈라 놓아서,
    업종 제한만으로는 한 테마 몰빵을 못 막는다 → 최근 20일 수익률 상관으로 한 번 더 거른다."""
    picks, per_sec = [], {}
    for r in cands:
        if len(picks) >= max_pos:
            break
        sec = r.get("sector") or "기타"
        if per_sec.get(sec, 0) >= MAX_PER_SECTOR:
            r["skip_reason"] = f"섹터 중복({sec})"; continue
        twin = None
        for q in picks:
            c = correlation(r.get("rets20"), q.get("rets20"))
            if c is not None and c >= CORR_LIMIT:
                twin = (q["name"], c); break
        if twin:
            r["skip_reason"] = f"{twin[0]}와 같이 움직임(상관 {twin[1]:.2f})"; continue
        picks.append(r); per_sec[sec] = per_sec.get(sec, 0) + 1
    return picks


def entry_window_ok(t_label: str) -> bool:
    """신규 진입 가능한 시간대인지 (개장 30분·점심 소강·장외 제외)."""
    return not any(x in t_label for x in ("❌", "⏸", "장외"))


def combine_thresholds(regime: dict, mgate: dict) -> dict:
    """레짐(breadth) 상향 + 지수 게이트 상향 합산. 둘은 상관이 커서 합산 상한을 둔다."""
    base = dict(GRADE)
    reg_extra = regime["thresholds"]["선도매수"] - base["선도매수"]
    extra = min(MAX_EXTRA_THRESHOLD, reg_extra + (mgate.get("extra") or 0))
    return {k: v + extra for k, v in base.items()}

# ══════════════════════════════════════════════════════════════════
# v15 핵심: 스코어 계산 (v14 뼈대 유지 + 휴림로봇형 오판 수정)
# ══════════════════════════════════════════════════════════════════
# [v14에서 하락 종목이 추천된 이유]
#  ② '5일 저점 근처 시가' +25점  → 떨어지는 종목일수록 저점 근처라 만점
#  ③ '눌림목 반등'(D-2 대비 -2~-15%) +20점 → 반등 확인 없이 '하락'만으로 만점
#  ① 거래량 가속 +30점 → 투매(거래량 실린 음봉)도 가속으로 가점
#  → 셋이 겹치면 하락 추세 종목이 75점(선도매수 기준 60점)을 넘는다.
#  추세·공시 필터가 없어 20·60일선이 꺾인 종목, 유상증자 공시 종목도 그대로 통과.
# [v15 수정]
#  · 차단 게이트: 데이터 이상값 / 거래정지·투자경고·위험·관리 / 악재 공시(DART) /
#    갭업 3%↑ / 당일 15%↑ / 거래대금 부족 / 20·60일선 아래이면서 그 선이 하락 중
#  · ② 위치: 저점이 아니라 '상승 추세 속 눌림 + 오늘 반등 확인'에만 만점
#  · ③ 눌림목 → 추세 점수로 교체 (정배열·20일선 상승)
#  · ① 거래량 실린 음봉은 -10점
#  · VWAP(당일 평균 매수가) 아래 -8점, 선도매수는 VWAP 위 + 거래량 1.5배↑ 필수
def calc_score_v15(daily: list, rt: dict, investor: dict, momentum_mode: bool = False,
                   strength: Optional[float] = None, filings: Optional[list] = None,
                   sector_avg: Optional[float] = None) -> dict:
    signals, warnings, blocks = [], [], []
    bd = {}
    now = _now()
    mst = market_status()
    today_str = now.strftime("%Y%m%d")

    # ── 확정 봉(hist)과 오늘 분리 ──
    hist = [d for d in daily if str(d.get("date", "")) < today_str] if daily else []
    base = {"total": 0, "bd": {}, "grade": "⛔차단", "signals": [], "warnings": [], "blocks": blocks,
            "vol_accel": 0, "range_pos": None, "gap_rate": 0, "rate_5d": 0,
            "change_rate": rt.get("change_rate") or 0.0, "inst_net": investor.get("inst_net"),
            "fore_net": rt.get("_frgn_ntby"), "prog_net": rt.get("_pgtr_ntby"), "mcap": rt.get("_mcap"),
            "vwap": rt.get("vwap"), "strength": strength, "ma20": None, "ma60": None}

    # ── 0. 데이터 검증 (v14 네이버 버그값 재발 방지) ──
    price = rt.get("price")
    prev_close = rt.get("prev_close") or (hist[0]["close"] if hist else None)
    if not price or not prev_close or len(hist) < 25:
        blocks.append("데이터 부족/조회 실패"); return base
    t_open = rt.get("today_open") or price
    t_high = rt.get("today_high") or max(price, t_open)
    t_low  = rt.get("today_low") or min(price, t_open)
    if not (t_low <= price <= t_high) or abs(price / prev_close - 1) > 0.31 or abs(t_open / prev_close - 1) > 0.31:
        blocks.append(f"시세 이상값 (현재 {price:,} 시 {t_open:,} 고 {t_high:,} 저 {t_low:,})"); return base

    closes = [d["close"] for d in hist]            # 최신 → 과거
    vols   = [d["volume"] for d in hist]
    def ma(n, shift=0):
        seg = closes[shift:shift + n]
        return sum(seg) / n if len(seg) == n else None
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    ma20_prev, ma60_prev = ma(20, 5), ma(60, 5)
    avg_vol = (sum(vols[:20]) / len(vols[:20])) or 1
    amts = [d.get("amount") or d["close"] * d["volume"] for d in hist[:20]]
    avg_amt_eok = sum(amts) / len(amts) / 1e8
    change_rate = rt.get("change_rate") if rt.get("change_rate") is not None else (price / prev_close - 1) * 100
    today_volume = rt.get("today_volume") or 0
    vwap = rt.get("vwap") or price
    mcap = rt.get("_mcap")
    bullish_today = price > t_open
    gap_rate = (t_open - prev_close) / prev_close
    d5_close = closes[4] if len(closes) >= 5 else closes[-1]
    rate_5d = (price - d5_close) / d5_close
    hi5 = max(d["high"] for d in hist[:5]); lo5 = min(d["low"] for d in hist[:5])
    range_pos_open = (t_open - lo5) / (hi5 - lo5) if hi5 > lo5 else 0.5
    pullback = (price / hi5 - 1) * 100             # 5일 고점 대비 (%)
    # v17: '이미 얼마나 달렸나' — 고득점 종목이 급등 말기였던 문제의 핵심 지표
    disparity = (price / ma20 - 1) * 100 if ma20 else 0.0          # 20일선 이격도 (%)
    lo20 = min(d["low"] for d in hist[:20])
    runup20 = (price / lo20 - 1) * 100 if lo20 else 0.0            # 20일 저점 대비 상승률 (%)
    atr = calc_atr(hist)
    atr_pct = atr / price * 100 if atr else None

    # ── 거래량 가속 (v12 U자 곡선 유지) ──
    if today_volume > 0 and mst == "OPEN":
        frac = max(expected_volume_fraction(elapsed_market_minutes()), 0.01)
        vol_accel = today_volume / frac / avg_vol
    elif today_volume > 0:
        vol_accel = today_volume / avg_vol
    else:
        vol_accel = 0

    # ── 1. 차단 게이트 ──
    if rt.get("temp_stop"):                      blocks.append("거래정지")
    if rt.get("stat_code") in BLOCK_STAT_CODES:  blocks.append(BLOCK_STAT_CODES[rt["stat_code"]])
    if rt.get("warn_code") in BLOCK_WARN_CODES:  blocks.append(BLOCK_WARN_CODES[rt["warn_code"]])
    if filings:                                  blocks.append("악재공시 " + filings[0])
    if gap_rate > 0.03:                          blocks.append(f"갭업 +{gap_rate*100:.1f}% — 고점 진입 금지")
    if change_rate >= HOT_INTRADAY_BLOCK:        blocks.append(f"당일 +{change_rate:.1f}% 급등 — 추격 금지")
    if avg_amt_eok < MIN_AVG_AMOUNT_EOK:         blocks.append(f"거래대금 부족 (20일 평균 {avg_amt_eok:.0f}억)")
    if disparity >= DISPARITY_BLOCK:             blocks.append(f"20일선 이격 +{disparity:.0f}% — 급등 후 되돌림 위험")
    elif runup20 >= RUNUP20_BLOCK:               blocks.append(f"20일 저점 대비 +{runup20:.0f}% — 급등 말기")
    if ma60 and ma60_prev and price < ma60 and ma60 < ma60_prev:
        blocks.append("하락 추세 (60일선 아래 + 60일선 하락)")
    elif ma20 and ma20_prev and price < ma20 and ma20 < ma20_prev:
        blocks.append("하락 추세 (20일선 아래 + 20일선 하락)")

    # ── ① 거래량 가속 (30) ──
    if vol_accel >= 1.5 and not bullish_today and price < prev_close:
        s1 = -10; warnings.append(f"거래량 {vol_accel:.1f}배 실린 음봉 — 매도 물량 출회")
    elif vol_accel >= 2.0: s1 = 30; signals.append(f"거래량 급가속 {vol_accel:.1f}배 🔥")
    elif vol_accel >= 1.5: s1 = 25; signals.append(f"거래량 가속 {vol_accel:.1f}배")
    elif vol_accel >= 1.0: s1 = 18
    elif vol_accel >= 0.5: s1 = 10
    else:                  s1 = 0; warnings.append("거래량 미약")
    bd["①거래량가속"] = s1

    # ── ② 진입 위치 (25) ──
    if momentum_mode:
        # 상승장: 상단 돌파·주도주 (v14 유지) — 단 오늘 양봉일 때만 인정
        vol_ok = vol_accel >= 1.2
        if range_pos_open >= 0.85:   s2 = 25 if vol_ok else 12; signals.append(f"신고가권 돌파 (레인지 {range_pos_open:.0%})")
        elif range_pos_open >= 0.70: s2 = 18 if vol_ok else 8;  signals.append(f"상단 강세 (레인지 {range_pos_open:.0%})")
        elif range_pos_open >= 0.50: s2 = 8
        else:                        s2 = 0
        if (not bullish_today or price <= prev_close) and s2 > 0:
            s2 //= 2; warnings.append("돌파 후 음봉/하락 — 힘 약화")
        if disparity >= DISPARITY_WARN and s2 > 8:
            s2 = 8                                   # 이미 달린 종목의 '신고가 돌파'는 추격이다
    else:
        # 조정장: 상승 추세 속 눌림 + '오늘 반등 확인'에만 점수
        if -12 <= pullback <= -3 and bullish_today and price > prev_close:
            s2 = 25; signals.append(f"눌림({pullback:.1f}%) 후 반등 확인")
        elif -12 <= pullback <= -3 and bullish_today:
            s2 = 15; signals.append(f"눌림({pullback:.1f}%) 후 양봉")
        elif -12 <= pullback <= -3:
            s2 = 5;  warnings.append("눌림 구간이지만 반등 미확인")
        elif pullback < -12:
            s2 = 0;  warnings.append(f"5일 고점 대비 {pullback:.1f}% — 눌림 아닌 하락")
        else:
            s2 = 8 if bullish_today else 0
    bd["②위치"] = s2

    # ── ③ 추세 (20) — v14 '눌림목(D-2 대비 하락)' 대체 ──
    if ma5 and ma20 and ma60 and price > ma5 > ma20 > ma60:
        s3 = 20; signals.append("정배열 (현재가>5>20>60일선)")
    elif ma20 and ma20_prev and price > ma20 and ma20 >= ma20_prev:
        s3 = 14; signals.append("20일선 위 + 20일선 상승")
    elif ma20 and price > ma20:
        s3 = 8
    else:
        s3 = 0
    bd["③추세"] = s3

    # ── ④ 연속 양봉 (10, v14 유지) ──
    bull = 0
    for d in hist:
        if d["close"] > d["open"]: bull += 1
        else: break
    s4 = {1: 4, 2: 10, 3: 6}.get(bull, 0)      # v17: 3일 연속은 이미 늦은 쪽 — 2일째가 최고점
    if bull >= 4: warnings.append(f"연속양봉 {bull}일 — 과열 구간")
    bd["④연속양봉"] = s4

    # ── ⑤ 수급 (20) — v14 + 3일 누적 + 체결강도 ──
    prog_net, fore_net = rt.get("_pgtr_ntby"), rt.get("_frgn_ntby")
    inst_net = investor.get("inst_net")
    s5 = 0
    prog_eok = (prog_net or 0) * price / 1e8      # v17: 금액(억원) 기준 — 5만주는 주가에 따라 2억도 40억도 된다
    if prog_net is not None:
        if   prog_eok >=  PROG_BIG_EOK: s5 += 10; signals.append(f"프로그램 대량 순매수 {prog_eok:+.0f}억")
        elif prog_net >      0: s5 += 6;  signals.append(f"프로그램 순매수 {prog_eok:+.1f}억")
        elif prog_eok <= -PROG_BIG_EOK: s5 -= 8;  warnings.append(f"프로그램 대량 순매도 {prog_eok:+.0f}억")
        elif prog_net <      0: s5 -= 4
    if fore_net is not None:
        if fore_net > 0: s5 += 6; signals.append(f"외국인 순매수 {fore_net:+,}")
        elif fore_net < 0: s5 -= 3
    if inst_net is not None:
        if inst_net > 0: s5 += 5
        elif inst_net * price < -3e8: s5 -= 4       # 기관 3억원↑ 순매도
    if (prog_net or 0) > 0 and (fore_net or 0) > 0:
        s5 += 4; signals.append("프로그램·외국인 동반 유입 🔥")
    i3, f3 = investor.get("inst_3d"), investor.get("fore_3d")
    if i3 is not None and f3 is not None:
        if i3 > 0 and f3 > 0:   s5 += 4; signals.append("기관·외국인 3일 동반 순매수")
        elif i3 < 0 and f3 < 0: s5 -= 4; warnings.append("기관·외국인 3일 동반 순매도")
    if strength is not None:
        if strength >= 120:  s5 += 4; signals.append(f"체결강도 {strength:.0f}")
        elif strength < 80:  s5 -= 5; warnings.append(f"체결강도 {strength:.0f} — 매도 우위")
    s5 = max(-12, min(20, s5))
    bd["⑤수급"] = s5

    # ── ⑥ 과열·위험 페널티 (v14 유지 + VWAP·경고종목) ──
    s6 = 0
    if   rate_5d >= 0.25 and vol_accel >= 2.0: s6 -= 25; warnings.append("5일+25%+거래량2배 — 극도 과열 🔴")
    elif rate_5d >= 0.20:                       s6 -= 15; warnings.append(f"5일 {rate_5d*100:.0f}% 과열")
    elif rate_5d >= 0.15:                       s6 -= 8
    if   change_rate >= HOT_INTRADAY_WARN:      s6 -= 15; warnings.append(f"당일 {change_rate:.1f}% 급등 — 고점 추격 위험")
    elif change_rate >= HOT_INTRADAY_CAUTION:   s6 -= 7;  warnings.append(f"당일 {change_rate:.1f}% 상승 — 진입가 불리")
    elif change_rate > 5:                       s6 -= 10; warnings.append(f"당일 이미 +{change_rate:.1f}% — 뒤늦은 진입")
    elif change_rate > 3:                       s6 -= 5
    if disparity >= DISPARITY_WARN:
        s6 -= 10; warnings.append(f"20일선 이격 +{disparity:.0f}% — 이미 달린 종목")
    rng = t_high - t_low
    if rng > 0 and rng / prev_close > 0.03 and (t_high - max(price, t_open)) / rng > 0.5:
        s6 -= 8; warnings.append("윗꼬리 긺 — 매도 압력")
    if price < vwap:
        s6 -= 8; warnings.append(f"VWAP {vwap:,.0f} 아래 — 오늘 산 사람 평균 손실 구간")
    if rt.get("warn_code") == "01" or rt.get("invt_caful"):
        s6 -= 10; warnings.append("투자주의/유의 종목")
    if rt.get("short_over"):
        s6 -= 10; warnings.append("단기과열 지정")
    bd["⑥과열페널티"] = s6

    # ── ⑦ 섹터 동조 (5) — 가이드 '대장주 동조화' 원칙 ──
    s7 = 0
    if sector_avg is not None:
        if sector_avg > 0: s7 = 5; signals.append(f"섹터 동반 상승 {sector_avg:+.1f}%")
        elif sector_avg < -1: warnings.append(f"섹터 약세 {sector_avg:+.1f}% — 단독 움직임 주의")
    bd["⑦섹터"] = s7

    total = max(-10, min(100, sum(bd.values())))
    grade = decide_grade(total, GRADE)

    # ── 시총 필터 (v14 유지) ──
    if MCAP_FILTER_ON and mcap is not None:
        if mcap < MCAP_MIN:   grade = "⬇제외"; warnings.append(f"시총 {mcap:,}억 — 필터 하한 미달")
        elif mcap > MCAP_MAX: grade = "⬇제외"; warnings.append(f"시총 {mcap:,}억 — 대형주(단타 전략 외)")

    base.update({
        "total": round(total, 1), "bd": bd, "grade": grade, "signals": signals, "warnings": warnings,
        "vol_accel": round(vol_accel, 2), "range_pos": round(range_pos_open, 3),
        "gap_rate": round(gap_rate * 100, 2), "rate_5d": round(rate_5d * 100, 2),
        "change_rate": change_rate, "inst_net": inst_net, "fore_net": fore_net, "prog_net": prog_net,
        "ma20": round(ma20) if ma20 else None, "ma60": round(ma60) if ma60 else None,
        "pullback": round(pullback, 1), "vwap": vwap, "bullish_today": bullish_today, "bull_streak": bull,
        "disparity": round(disparity, 1), "runup20": round(runup20, 1),
        "atr_pct": round(atr_pct, 2) if atr_pct else None, "rets20": daily_returns(hist),
    })
    return finalize_grade(base, GRADE)


def lead_disqualifiers(sc: dict) -> list:
    """v16.1: 점수가 높아도 '선도매수'가 될 수 없는 사유. 페널티는 점수를 깎을 뿐 아니라 등급 상한이 된다.
    (10/2 실전 스캔: 극도과열·당일 +10%·체결강도 69인 종목까지 선도매수로 떠서 28개가 됨)"""
    why = []
    if not sc.get("vol_accel", 0) >= 1.5:
        why.append("거래량 1.5배 미만")
    if not sc.get("bullish_today"):
        why.append("오늘 음봉")
    if any("VWAP" in w for w in sc["warnings"]):
        why.append("VWAP 아래")
    if (sc.get("change_rate") or 0) >= HOT_INTRADAY_CAUTION:
        why.append(f"당일 +{sc['change_rate']:.1f}% (7%↑ 추격)")
    if (sc.get("rate_5d") or 0) >= 20:
        why.append(f"5일 +{sc['rate_5d']:.0f}% 과열")
    if (sc.get("bull_streak") or 0) >= 4:
        why.append(f"연속양봉 {sc['bull_streak']}일")
    if sc.get("strength") is not None and sc["strength"] < 100:
        why.append(f"체결강도 {sc['strength']:.0f}")
    if any(w.startswith("프로그램 대량 순매도") for w in sc["warnings"]):
        why.append("프로그램 대량 순매도")
    if any(w.startswith("20일선 이격") for w in sc["warnings"]):
        why.append("20일선 이격 과다")
    if sc.get("atr_pct") and sc["atr_pct"] > STOP_CAP_PCT:
        why.append(f"변동성 과다 (하루 변동폭 {sc['atr_pct']:.1f}% > 손절상한 {STOP_CAP_PCT:.0f}%)")
    if any(w.startswith("윗꼬리") for w in sc["warnings"]):
        why.append("윗꼬리")
    if any(w.startswith("섹터 약세") for w in sc["warnings"]):
        why.append("섹터 약세")
    if any("3일 동반 순매도" in w for w in sc["warnings"]):
        why.append("기관·외국인 매도")
    return why


def finalize_grade(sc: dict, thresholds: dict) -> dict:
    """등급 확정: 차단 > 시총 제외 > 점수(레짐 임계값) > 품질 조건.
    v17: 품질 조건에 하나라도 걸리면 '관심'까지만 — 화면의 선도매수·매수대기는 전부 조건 통과 종목."""
    if sc["blocks"]:
        sc["grade"] = "⛔차단"; return sc
    if any(w.startswith("시총 ") for w in sc["warnings"]):
        sc["grade"] = "⬇제외"; return sc
    g = decide_grade(sc["total"], thresholds)
    sc["lead_block"] = []
    if g in ("🚀선도매수", "⬆매수대기"):
        why = lead_disqualifiers(sc)
        sc["lead_block"] = why
        if why:
            g = "👀관심"
    sc["grade"] = g
    return sc


def cap_lead_count(results: list, max_lead: int) -> int:
    """선도매수는 점수 상위 max_lead개만. 나머지는 매수대기로 (3종목 운용에 28개 신호는 의미 없음)."""
    leads = sorted([r for r in results if r["grade"] == "🚀선도매수"], key=lambda r: -r["score"])
    for r in leads[max_lead:]:
        r["grade"] = "⬆매수대기"
        r.setdefault("lead_block", []).append(f"선도 정원 초과(상위 {max_lead}개만)")
    return min(len(leads), max_lead)


def fetch_market_breadth() -> Optional[dict]:
    """코스피·코스닥 전체 상승/하락 종목 수 (KIS 업종 현재가). 스캔 대상은 '오늘 거래 상위'라
    상승 쪽으로 편향돼 있어서, 레짐은 시장 전체 등락 종목 수로 판정해야 한다."""
    up = down = flat = 0
    for code in ("0001", "1001"):
        try:
            o = _KIS.get("/uapi/domestic-stock/v1/quotations/inquire-index-price", "FHPUP02100000",
                         {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code})["output"]
        except Exception:
            return None
        a, d, f = _f(o.get("ascn_issu_cnt")), _f(o.get("down_issu_cnt")), _f(o.get("stnr_issu_cnt"), 0)
        if a is None or d is None:
            return None
        up += a; down += d; flat += f
    tot = up + down + flat
    return {"up": int(up), "down": int(down), "ratio": up / tot} if tot > 0 else None


def assess_regime_market(scored: list, mst: str) -> dict:
    """시장 전체 등락 종목 비율로 레짐 판정. 실패하면 스캔 대상 기준(편향 보정: 우호 기준 60%)."""
    base = dict(GRADE)
    if mst != "OPEN":
        return assess_regime(scored, mst)
    br = fetch_market_breadth()
    if br:
        ratio, src = br["ratio"], f"시장 전체 상승 {br['up']:,} / 하락 {br['down']:,}"
        cut_ok, cut_mid = 0.50, 0.35
    else:
        rates = [sc.get("change_rate") for *_, sc in scored if sc.get("change_rate") is not None]
        if len(rates) < 5:
            return assess_regime(scored, mst)
        ratio = sum(1 for r in rates if r > 0) / len(rates)
        src, cut_ok, cut_mid = "스캔 대상 기준(상승 편향 보정)", 0.60, 0.45
    if ratio >= cut_ok:
        label, th = "🟢단타우호", base
    elif ratio >= cut_mid:
        label, th = "🟡중립경계", {k: v + 10 for k, v in base.items()}
    else:
        label, th = "🔴단타비우호", {k: v + 20 for k, v in base.items()}
    return {"label": label, "desc": f"{src} — 상승 비율 {ratio:.0%}", "thresholds": th,
            "adv_ratio": ratio, "accel_ratio": None}

# ══════════════════════════════════════════════════════════════════
# 엑셀 연동
# ══════════════════════════════════════════════════════════════════
# 스타일
def fill(h): return PatternFill("solid", fgColor=h)
F_NAVY  = fill("1B3A5C"); F_YELL = fill("FFF2CC")
F_GREEN = fill("E2EFDA"); F_BLUE = fill("DAEEF3")
F_UP    = fill("FFE0E0"); F_DN   = fill("E0E8FF")
F_WARN  = fill("FFC7CE"); F_OK   = fill("C6EFCE")
DATA_FONT = Font(name="맑은 고딕", size=9)
HDR_FONT  = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=9)
ALIGN_C   = Alignment(horizontal="center", vertical="center")

def find_workbook() -> Optional[Path]:
    candidates = [
        p for p in SCRIPT_DIR.glob("단타매수대상종목*.xlsx")
        if not p.name.startswith("~$") and not p.name.startswith(".~")
    ]
    if not candidates:
        print("[!] 단타매수대상종목*.xlsx 파일을 찾을 수 없습니다.")
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


COL = {
    "name":1, "code":2, "open":3, "high":4, "low":5,
    "c0":6, "c1":7, "c2":8, "c3":9, "c4":10,
    "v0":11,"v1":12,"v2":13,"v3":14,"v4":15,
    "naver":16,
    # v11 실시간 컬럼
    "price":17, "change":18, "gap":19,
    "vol_accel":20, "range_pos":21,
    "inst":22, "fore":23,
    "score":24, "grade":25,
    "signals":26, "warnings":27,
    "updated":28,
}

RT_HEADERS_V11 = {
    17: "현재가",      18: "등락률(%)",   19: "갭업률(%)",
    20: "거래량가속",  21: "레인지위치",
    22: "기관순매수",  23: "외국인순매수",
    24: "v11점수",     25: "판정",
    26: "매수신호",    27: "경고",        28: "갱신시각",
}

def ensure_headers(ws):
    for col, label in RT_HEADERS_V11.items():
        c = ws.cell(4, col)
        if c.value != label:
            c.value = label; c.fill = F_NAVY; c.font = HDR_FONT; c.alignment = ALIGN_C


def write_row(ws, row, d0, sc, now_str):
    def w(col, val, bg=F_YELL):
        c = ws.cell(row, col, val)
        c.fill = bg; c.font = DATA_FONT; c.alignment = ALIGN_C

    # 일봉
    ws.cell(row, COL["open"],  d0["open"])
    ws.cell(row, COL["high"],  d0["high"])
    ws.cell(row, COL["low"],   d0["low"])

    # 실시간
    chg = sc.get("change_rate", 0)
    chg_bg = F_UP if chg > 0 else (F_DN if chg < 0 else F_YELL)

    w(COL["price"],    sc.get("price",    d0["close"]))
    w(COL["change"],   round(chg, 2),                   chg_bg)
    w(COL["gap"],      sc.get("gap_rate", 0),            F_WARN if sc.get("gap_rate",0) > 3 else F_YELL)
    w(COL["vol_accel"],sc.get("vol_accel", 0),           F_OK if sc.get("vol_accel",0) >= 1.5 else F_YELL)
    w(COL["range_pos"],sc.get("range_pos", 0),           F_OK if sc.get("range_pos",0) <= 0.35 else F_YELL)
    w(COL["inst"],     sc.get("inst_net"),               F_GREEN)
    w(COL["fore"],     sc.get("fore_net"),               F_BLUE)

    score_bg = F_OK if sc["total"] >= 45 else (F_WARN if sc["total"] >= 28 else F_YELL)
    w(COL["score"],    sc["total"],                      score_bg)
    w(COL["grade"],    sc["grade"],                      score_bg)
    w(COL["signals"],  " | ".join(sc["signals"][:3]),    F_OK if sc["signals"] else F_YELL)
    w(COL["warnings"], " | ".join(sc["warnings"][:2]),   F_WARN if sc["warnings"] else F_YELL)
    w(COL["updated"],  now_str,                          F_YELL)


# ══════════════════════════════════════════════════════════════════
# v13 신규: 자금관리 · 포지션 추천
# ══════════════════════════════════════════════════════════════════
def load_sector_map(ws_pool) -> dict:
    """종목풀 시트의 대분류섹터(F열)를 code6 → 섹터명 으로 매핑"""
    m = {}
    for r in range(2, ws_pool.max_row + 1):
        code = ws_pool.cell(r, 2).value
        sector = ws_pool.cell(r, 6).value
        if not code:
            continue
        try:
            code6 = norm_code(code)
        except Exception:
            continue
        m[code6] = sector or "기타"
    return m


def size_position(r: dict, capital: float, max_pos: int, stop_pct: float, profit_pct: float,
                  risk_pct: float = RISK_PER_TRADE_PCT) -> Optional[dict]:
    """v16: 손절가까지 떨어져도 종목당 손실이 자본의 risk_pct% 를 넘지 않게 수량 결정.
    손절·목표가는 r["plan"](ATR 기반, 호가단위 반영)이 있으면 그것을, 없으면 고정 %를 쓴다."""
    price = r.get("price")
    if not price:
        return None
    plan = r.get("plan") or {}
    stop_price = plan.get("stop") or round_tick(price * (1 - stop_pct / 100), up=False)
    tgt_price = plan.get("target") or round_tick(price * (1 + profit_pct / 100), up=True)
    per_share_risk = max(price - stop_price, tick_size(price))
    by_alloc = int((capital / max(1, max_pos)) // price)
    by_risk = int((capital * risk_pct / 100) // per_share_risk)
    shares = min(by_alloc, by_risk)
    if shares <= 0:
        return None
    invested = shares * price
    return {**r, "shares": shares, "invested": invested, "stop_price": stop_price, "target_price": tgt_price,
            "max_loss": shares * (price - stop_price), "max_gain": shares * (tgt_price - price),
            "stop_pct": round((price - stop_price) / price * 100, 2),
            "target_pct": round((tgt_price / price - 1) * 100, 2),
            "atr_pct": plan.get("atr_pct"), "basis": plan.get("basis", "고정%"),
            "limit": "위험한도" if by_risk < by_alloc else "배분한도"}


def recommend_portfolio(results: list, ws_pool, capital: float, max_pos: int,
                         stop_pct: float, profit_pct: float, verbose: bool = True) -> list:
    """
    스캔 결과에서 등급·점수·섹터분산 기준으로 최대 max_pos개 종목을 골라
    자본을 균등배분하고, 종목당 매수수량·손절가·목표가·최대손익을 계산한다.
    """
    sector_map = load_sector_map(ws_pool)

    primary  = [r for r in results if r.get("price") and r["grade"] in ("🚀선도매수", "⬆매수대기")]
    fallback = []          # v17: 관심 등급으로 억지로 채우지 않음 (모자라면 현금)
    primary.sort(key=lambda x: x["score"], reverse=True)
    fallback.sort(key=lambda x: x["score"], reverse=True)

    picks, sector_count, seen = [], {}, set()
    for r in primary + fallback:
        if len(picks) >= max_pos:
            break
        if r["code"] in seen:
            continue
        sec = sector_map.get(r["code"], "기타")
        if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
            continue
        picks.append(r)
        seen.add(r["code"])
        sector_count[sec] = sector_count.get(sec, 0) + 1

    if not picks:
        if verbose:
            print("\n  ℹ️  현재 조건을 충족하는 매수 후보가 없습니다. 무리한 진입보다 현금 보유를 권장합니다.")
        return []

    rows = []
    total_invest = total_maxloss = total_maxgain = 0.0
    for r in picks:
        row = size_position({**r, "sector": sector_map.get(r["code"], r.get("sector") or "기타")},
                            capital, max_pos, stop_pct, profit_pct)
        if not row:
            continue
        rows.append(row)
        total_invest += row["invested"]
        total_maxloss += row["max_loss"]
        total_maxgain += row["max_gain"]

    # 손익분기 승률: ATR 계획의 평균 손절/목표 폭 기준
    if rows:
        stop_pct = sum(r["stop_pct"] for r in rows) / len(rows)
        profit_pct = sum(r["target_pct"] for r in rows) / len(rows)
    breakeven_wr = stop_pct / (stop_pct + profit_pct) * 100
    cost_wr_bump = TOTAL_FRICTION_PCT / (stop_pct + profit_pct) * 100

    if verbose:
        n = len(rows)
        print(f"\n{'═'*72}")
        print(f"  💰 오늘의 추천 포트폴리오  (자본 {capital:,.0f}원 / 목표 {max_pos}종목 / 손절{stop_pct:.1f}%·익절{profit_pct:.1f}%)")
        print(f"{'═'*72}")
        print(f"  {'종목명':14} {'섹터':10} {'현재가':>9} {'수량':>6} {'투입금액':>11} {'손절가':>9} {'목표가':>9}")
        print(f"  {'─'*14} {'─'*10} {'─'*9} {'─'*6} {'─'*11} {'─'*9} {'─'*9}")
        for r in rows:
            print(f"  {r['name']:14} {r['sector']:10} {r['price']:>9,} {r['shares']:>6,} "
                  f"{r['invested']:>11,} {r['stop_price']:>9,} {r['target_price']:>9,}")
        unused = capital - total_invest
        print(f"  {'─'*72}")
        print(f"  총 투입: {total_invest:,.0f}원  (미투입 현금 {unused:,.0f}원)")
        if total_invest > 0:
            print(f"  전종목 손절 시 최대손실: -{total_maxloss:,.0f}원 (자본 대비 -{total_maxloss/capital*100:.1f}%)")
            print(f"  전종목 익절 시 기대이익: +{total_maxgain:,.0f}원 (자본 대비 +{total_maxgain/capital*100:.1f}%)")
        print(f"  손익분기 승률(세전): {breakeven_wr:.1f}%  →  거래세·수수료·슬리피지(왕복 약 {TOTAL_FRICTION_PCT:.2f}%) "
              f"반영 시 약 {breakeven_wr + cost_wr_bump:.1f}% 이상 필요")
        if n < max_pos:
            print(f"  ⚠️  조건 충족 종목이 {n}개뿐 — 나머지는 현금 보유 권장 (억지로 {max_pos}종목 채우지 말 것)")
        print(f"{'═'*72}\n")

    return rows


REC_HEADERS = ["종목명","코드","섹터","등급","점수","현재가","수량","투입금액",
               "손절가","목표가","최대손실","최대이익","갱신시각"]

def write_recommendation_sheet(wb, rows: list, capital: float, stop_pct: float,
                                profit_pct: float, now_str: str):
    sheet_name = "포지션추천"
    if sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        ws.delete_rows(1, ws.max_row)
    else:
        ws = wb.create_sheet(sheet_name)

    ws.cell(1, 1, f"오늘의 추천 포트폴리오  (자본 {capital:,.0f}원 · 손절{stop_pct:.1f}% · 익절{profit_pct:.1f}% · {now_str})")
    for col, label in enumerate(REC_HEADERS, start=1):
        c = ws.cell(3, col, label)
        c.fill = F_NAVY; c.font = HDR_FONT; c.alignment = ALIGN_C

    for i, r in enumerate(rows, start=4):
        vals = [r["name"], r["code"], r["sector"], r["grade"], r["score"], r["price"],
                r["shares"], r["invested"], r["stop_price"], r["target_price"],
                round(r["max_loss"]), round(r["max_gain"]), now_str]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(i, col, v)
            c.fill = F_YELL; c.font = DATA_FONT; c.alignment = ALIGN_C


# ══════════════════════════════════════════════════════════════════
# v14 신규: 매매일지 누적 로그 + 성과 리포트
# ══════════════════════════════════════════════════════════════════
LOG_SHEET   = "매매일지"
LOG_HEADERS = ["실행일자","실행시각","종목명","코드","섹터","등급","점수",
               "추천가","수량","투입금액","손절가","목표가",
               "실제진입","진입가","청산일시","청산가",
               "실현손익액","실현손익률(%)","결과","메모",
               "자동결과","자동손익률(%)","최대상승(%)","최대하락(%)"]   # v15: --review 가 채움


def _ensure_log_header(ws):
    need = False
    if ws.max_row < 1:
        need = True
    else:
        for col, label in enumerate(LOG_HEADERS, start=1):
            if ws.cell(1, col).value != label:
                need = True
                break
    if need:
        for col, label in enumerate(LOG_HEADERS, start=1):
            c = ws.cell(1, col, label)
            c.fill = F_NAVY; c.font = HDR_FONT; c.alignment = ALIGN_C


def append_trade_log(wb, rows: list, now_dt: datetime) -> int:
    """
    rec_rows(추천 종목)를 [매매일지]에 누적 기록한다. 같은 날 이미 기록된 종목은
    중복 추가하지 않는다(스캔 주기마다 append하면 로그가 폭발하는 것 방지).
    실제진입~메모(수동 입력 컬럼)는 빈 칸으로 남겨두고, 사용자가 모의/실거래 결과를
    직접 채운 뒤 --report 로 집계한다.
    """
    ws = wb[LOG_SHEET] if LOG_SHEET in wb.sheetnames else wb.create_sheet(LOG_SHEET)
    _ensure_log_header(ws)

    today_str = now_dt.strftime("%Y-%m-%d")
    time_str  = now_dt.strftime("%H:%M")
    code_idx  = LOG_HEADERS.index("코드") + 1
    date_idx  = LOG_HEADERS.index("실행일자") + 1

    logged_today = set()
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, date_idx).value == today_str:
            c = ws.cell(r, code_idx).value
            if c: logged_today.add(str(c))

    appended = 0
    next_row = ws.max_row + 1
    for r in rows:
        if r["code"] in logged_today:
            continue
        vals = [today_str, time_str, r["name"], r["code"], r["sector"], r["grade"], r["score"],
                r["price"], r["shares"], r["invested"], r["stop_price"], r["target_price"],
                "", "", "", "", "", "", "", "", "", "", "", ""]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(next_row, col, v)
            c.fill = F_YELL; c.font = DATA_FONT; c.alignment = ALIGN_C
        next_row += 1
        appended += 1
        logged_today.add(r["code"])

    return appended


def print_trade_report(xlsx: Path):
    """[매매일지]의 '결과' 컬럼이 채워진 기록을 집계해 승률·평균손익·기대값을 출력한다."""
    try:
        wb = openpyxl.load_workbook(xlsx, data_only=True)
    except PermissionError:
        print("[!] 파일이 Excel에서 열려 있습니다. 닫고 다시 실행하세요.")
        return
    if LOG_SHEET not in wb.sheetnames:
        print(f"[!] [{LOG_SHEET}] 시트가 없습니다. 프로그램을 몇 차례 실행해 추천 기록부터 쌓으세요.")
        return

    ws = wb[LOG_SHEET]
    idx = {h: i + 1 for i, h in enumerate(LOG_HEADERS)}

    records = []
    for r in range(2, ws.max_row + 1):
        result = ws.cell(r, idx["결과"]).value
        if not result:
            continue
        records.append({
            "result": str(result).strip(),
            "pnl":    to_float(ws.cell(r, idx["실현손익률(%)"]).value),
            "grade":  ws.cell(r, idx["등급"]).value,
            "name":   ws.cell(r, idx["종목명"]).value,
        })

    if not records:
        print(f"[!] [{LOG_SHEET}]의 '결과' 컬럼이 채워진 기록이 없습니다. "
              f"모의/실거래 후 익절·손절·보류·미진입 을 직접 기입하세요.")
        return

    closed = [r for r in records if r["result"] in ("익절", "손절") and r["pnl"] is not None]
    wins   = [r for r in closed if r["result"] == "익절"]
    losses = [r for r in closed if r["result"] == "손절"]
    others = len(records) - len(closed)

    print(f"\n{'═'*60}")
    print(f"  📊 매매일지 리포트  (총 기록 {len(records)}건)")
    print(f"{'═'*60}")
    print(f"  청산완료 {len(closed)}건  (익절 {len(wins)} / 손절 {len(losses)})   기타(보류·미진입 등) {others}건")

    if closed:
        win_rate   = len(wins) / len(closed) * 100
        avg_win    = (sum(r["pnl"] for r in wins) / len(wins)) if wins else 0.0
        avg_loss   = (sum(r["pnl"] for r in losses) / len(losses)) if losses else 0.0
        expectancy = (win_rate/100) * avg_win + (1 - win_rate/100) * avg_loss
        print(f"  승률: {win_rate:.1f}%   평균 익절: +{avg_win:.2f}%   평균 손절: {avg_loss:.2f}%")
        verdict = "플러스 기대값" if expectancy > 0 else "마이너스 기대값 — 시스템 재점검 필요"
        print(f"  거래당 기대값: {expectancy:+.2f}%  ({verdict})")

        by_grade = {}
        for r in closed:
            by_grade.setdefault(r["grade"], []).append(r)
        if len(by_grade) > 1:
            print(f"\n  등급별 성과:")
            for g, rs in by_grade.items():
                w = [x for x in rs if x["result"] == "익절"]
                print(f"    {g}: {len(rs)}건, 승률 {len(w)/len(rs)*100:.0f}%")
    else:
        print("  청산 완료 기록이 아직 없습니다 — '결과' 컬럼에 익절/손절을 채워 넣으세요.")
    print(f"{'═'*60}\n")


# ══════════════════════════════════════════════════════════════════
# 메인 업데이트
# ══════════════════════════════════════════════════════════════════
def write_scanner_sheet(wb, results: list, regime: dict, now_str: str):
    """
    (1) 시스템 통합: 파이썬 엔진이 계산한 점수(시총·수급·레짐·모멘텀 반영)를
    그대로 보여주는 '단타추천(엔진)' 시트. 엑셀 수식 채점과 독립 — 네가 보는 화면이
    실제 엔진을 반영하게 만드는 핵심. 값은 전부 파이썬이 직접 기록(수식 없음).
    """
    SHEET = "🎯단타추천"
    if SHEET in wb.sheetnames:
        ws = wb[SHEET]
        ws.delete_rows(1, ws.max_row)
    else:
        ws = wb.create_sheet(SHEET, 0)   # 맨 앞 탭

    # 제외 등급 빼고 점수순 정렬
    rows = [r for r in results if r.get("grade") not in ("⬇제외", "⛔차단")]
    rows.sort(key=lambda x: x.get("score", 0), reverse=True)

    reg_label = regime.get("label", "-") if regime else "-"
    ws.cell(1, 1, f"🎯 단타 추천 v16 — KIS 실시간 (추세·공시·VWAP 게이트 반영)   |   레짐: {reg_label}   |   {now_str}")
    ws.cell(1, 1).font = Font(name="맑은 고딕", bold=True, size=12, color="FFFFFF")
    ws.cell(1, 1).fill = F_NAVY

    headers = ["순위","종목명","코드","섹터","종합점수","판정","시총(억)",
               "거래량가속","레인지","당일%","프로그램순매수","외국인","수급점수","신호","경고"]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(3, c, h)
        cell.font = HDR_FONT; cell.fill = F_NAVY; cell.alignment = ALIGN_C

    def fmt(v, kind=""):
        if v is None: return "-"
        if kind == "num": return f"{v:,}"
        return v

    r0 = 4
    for i, r in enumerate(rows, 1):
        grade = r.get("grade", "")
        score = r.get("score", 0)
        score_bg = F_OK if score >= 45 else (F_WARN if score >= 28 else F_YELL)
        vals = [
            i, r.get("name",""), r.get("code",""), r.get("sector",""),
            score, grade, fmt(r.get("mcap"), "num"),
            r.get("vol_accel"), r.get("range_pos"), r.get("change_rate"),
            fmt(r.get("prog_net"), "num"), fmt(r.get("fore_net"), "num"),
            r.get("supply_score"),
            " | ".join(r.get("signals", [])[:3]),
            " | ".join(r.get("warnings", [])[:2]),
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r0, c, v)
            cell.font = DATA_FONT; cell.alignment = ALIGN_C
            if c == 5 or c == 6:   # 점수/판정 강조
                cell.fill = score_bg
            elif c == 11 and isinstance(r.get("prog_net"), int):  # 프로그램 순매수 색
                cell.fill = F_GREEN if r["prog_net"] > 0 else (F_DN if r["prog_net"] < 0 else F_YELL)
            else:
                cell.fill = F_YELL
        r0 += 1

    if not rows:
        ws.cell(r0, 1, "조건 충족 종목 없음 (레짐/시총/과열 필터 적용 결과)")

    # 열 너비
    widths = [5,16,9,12,9,11,10,10,8,8,14,12,9,40,28]
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64+c) if c<=26 else 'A'].width = w


def run_update(xlsx: Path, verbose: bool = True,
                capital: float = DEFAULT_CAPITAL, max_pos: int = DEFAULT_MAX_POS,
                stop_pct: float = DEFAULT_STOP_PCT, profit_pct: float = DEFAULT_PROFIT_PCT) -> list:
    global STOP_CAP_PCT
    STOP_CAP_PCT = stop_pct
    init_feeds()
    mst = market_status()
    now_str = datetime.now().strftime("%H:%M")
    t_label, t_desc = current_time_strategy()

    print(f"\n{'═'*68}")
    print(f"  단타 스캐너 v17  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"  시장: {'장중 ★' if mst=='OPEN' else ('장전' if mst=='BEFORE' else '장마감')}")
    print(f"  현재 전략: {t_label} — {t_desc}")
    print(f"{'═'*68}")

    try:
        wb = openpyxl.load_workbook(xlsx)
    except PermissionError:
        print("[!] 파일이 Excel에서 열려 있습니다. 닫고 다시 실행하세요.")
        return []

    if "종목풀" not in wb.sheetnames or "일봉입력" not in wb.sheetnames:
        print("[!] 필요한 시트(종목풀/일봉입력)가 없습니다.")
        return []

    ws_pool  = wb["종목풀"]
    ws_daily = wb["일봉입력"]
    ensure_headers(ws_daily)
    sector_map = load_sector_map(ws_pool)
    cache = DayCache()
    today_str = datetime.now().strftime("%Y%m%d")

    results = []
    n_ok = n_fail = 0
    seen_codes = {}
    t0 = time.time()
    calls0 = _KIS.calls

    # ── Pass 1: 데이터 수집 ────────────────────────────────────────────
    collected = []  # (daily_row, name, code6, hist, rt, inv, filings)
    for pool_r in range(2, ws_pool.max_row + 1):
        name = ws_pool.cell(pool_r, 1).value
        code = ws_pool.cell(pool_r, 2).value
        if not name or not code: continue
        try: code6 = norm_code(code)
        except: continue
        if code6 in seen_codes:
            print(f"  [!] 종목코드 중복 {code6}: {seen_codes[code6]} / {name} → {name} 건너뜀 (종목풀 수정 필요)")
            continue
        seen_codes[code6] = str(name)
        daily_row = pool_r + 3

        try:
            hist = cache.get_or(f"d:{code6}", lambda: [d for d in (fetch_daily(code6) or []) if d["date"] < today_str] or None)
            rt = fetch_realtime(code6)
            inv = cache.get_or(f"i:{code6}", lambda: _safe(fetch_investor, code6, None)) or {}
            fil = cache.get_or(f"f:{code6}", lambda: _safe(fetch_filings, code6, None)) or []
        except KISError as e:
            print(f"  [✗] {str(name)[:16]:16s} ({code6}) {e}"); n_fail += 1; continue
        if not hist or not rt:
            print(f"  [✗] {str(name)[:16]:16s} ({code6}) 시세 없음"); n_fail += 1; continue
        collected.append((daily_row, str(name), code6, hist, rt, inv, fil))
        n_ok += 1
        if verbose and n_ok % 20 == 0:
            print(f"  ... {n_ok}종목 수집")
    cache.save()

    # 섹터 평균 등락률 (대장주 동조화)
    by_sec = {}
    for _, _, code6, _, rt, _, _ in collected:
        by_sec.setdefault(sector_map.get(code6, "기타"), []).append(rt.get("change_rate") or 0)
    sec_avg = {k: sum(v) / len(v) for k, v in by_sec.items() if len(v) >= 2}

    # 1차 채점 (레짐 판정용)
    scored = []
    for row in collected:
        daily_row, name, code6, hist, rt, inv, fil = row
        sc = calc_score_v15(hist, rt, inv, False, None, fil, sec_avg.get(sector_map.get(code6, "기타")))
        scored.append((daily_row, name, code6, hist, rt, sc))

    # ── 레짐 판정 (v12~v14 유지) ─────────────────────────────────────
    regime = assess_regime_market(scored, mst)
    mgate = assess_market_gate()
    th = combine_thresholds(regime, mgate)
    print(f"  📉 지수 게이트: {mgate['desc']}")
    print(f"\n{'━'*68}")
    print(f"  🧭 시장 레짐: {regime['label']}  —  {regime['desc']}")
    if th != GRADE:
        print(f"     임계값 조정: 선도매수≥{th['선도매수']}  매수대기≥{th['매수대기']}  관심≥{th['관심']}")
    if regime["label"] == "🔴단타비우호":
        print(f"     ⚠️  올라가는 종목이 적은 장입니다. 신규 단타는 쉬거나 비중을 크게 줄이세요.")
    momentum_on = ("우호" in regime.get("label", "") and "비우호" not in regime.get("label", "")
                   and not mgate.get("extra") and not mgate.get("block"))     # v17: 지수까지 양호할 때만
    if momentum_on:
        print(f"  🚀 모멘텀 모드 ON — 상승장: 상단 돌파·주도주 우선 채점")
    print(f"{'━'*68}")

    # ── Pass 2: 최종 채점 (후보만 체결강도 추가 조회) + 엑셀 기록 ─────────
    for (daily_row, name, code6, hist, rt, inv, fil), (_, _, _, _, _, sc0) in zip(collected, scored):
        sa = sec_avg.get(sector_map.get(code6, "기타"))
        strength = None
        if mst == "OPEN" and not sc0["blocks"] and sc0["total"] >= th["관심"] - 10:
            strength = _safe(fetch_strength, code6, None)
        sc = calc_score_v15(hist, rt, inv, momentum_on, strength, fil, sa)
        sc = finalize_grade(sc, th)

        # 일봉입력 시트 (D0 = 오늘 진행봉)
        bars = ([{"open": rt.get("today_open") or rt["price"], "high": rt.get("today_high") or rt["price"],
                  "low": rt.get("today_low") or rt["price"], "close": rt["price"],
                  "volume": rt.get("today_volume") or 0}] if mst == "OPEN" else []) + hist
        for i in range(min(5, len(bars))):
            ws_daily.cell(daily_row, COL["c0"] + i, bars[i]["close"])
            ws_daily.cell(daily_row, COL["v0"] + i, bars[i]["volume"])
        sc["price"] = rt["price"]
        write_row(ws_daily, daily_row, bars[0], sc, now_str)

        results.append({
            "name": name, "code": code6,
            "score": sc["total"], "grade": sc["grade"],
            "signals": sc["signals"], "warnings": sc["warnings"], "blocks": sc["blocks"],
            "vol_accel": sc.get("vol_accel"), "gap_rate": sc.get("gap_rate"),
            "change_rate": sc.get("change_rate"), "price": rt.get("price"),
            "inst_net": sc.get("inst_net"), "fore_net": sc.get("fore_net"),
            "prog_net": sc.get("prog_net"), "mcap": sc.get("mcap"),
            "range_pos": sc.get("range_pos"), "vwap": sc.get("vwap"), "strength": strength,
            "ma20": sc.get("ma20"), "ma60": sc.get("ma60"),
            "today_low": rt.get("today_low"), "lead_block": sc.get("lead_block", []),
            "plan": trade_plan(rt["price"], hist, stop_pct, rt.get("today_low")),
            "rets20": sc.get("rets20"), "disparity": sc.get("disparity"), "atr_pct": sc.get("atr_pct"),
            "supply_score": (sc.get("bd") or {}).get("⑤수급"),
            "bd": sc.get("bd"),
            "sector": sector_map.get(code6, ""),
        })

    print(f"\n  수집 {n_ok} / 실패 {n_fail} / KIS 호출 {_KIS.calls - calls0}회 / {time.time() - t0:.0f}초")
    try:
        wb.save(xlsx)
    except PermissionError:
        print("  ✗ 저장 실패 — Excel 파일을 닫고 다시 실행하세요.")

    n_lead = cap_lead_count(results, max(1, max_pos) * 2)
    print(f"  선도매수 {n_lead}개 (정원 = 보유 종목 수 × 2)")
    _print_summary(results, regime)

    try:
        write_scanner_sheet(wb, results, regime, now_str)
        write_blocked_sheet(wb, results, now_str)
        wb.save(xlsx)
    except PermissionError:
        print("  ✗ [🎯단타추천] 저장 실패 — Excel 파일을 닫고 다시 실행하세요.")

    if mgate.get("block"):
        print(f"\n  ⛔ 신규 진입 중단 — {mgate.get('block_reason')}. 오늘은 추천 포트폴리오를 만들지 않습니다.")
        rec_rows = []
    elif mst == "OPEN" and not entry_window_ok(t_label):
        print(f"\n  ⏸ 진입 시간대 아님({t_label}) — 추천 포트폴리오·매매일지 기록 생략.")
        rec_rows = []
    else:
        rec_rows = recommend_portfolio(results, ws_pool, capital, max_pos, stop_pct, profit_pct, verbose=verbose)
    if rec_rows:
        try:
            write_recommendation_sheet(wb, rec_rows, capital, stop_pct, profit_pct, now_str)
            appended = append_trade_log(wb, rec_rows, datetime.now())
            wb.save(xlsx)
            if appended:
                print(f"  📝 [매매일지]에 신규 {appended}건 기록 — 며칠 뒤 --review 로 자동 채점")
        except PermissionError:
            print("  ✗ [포지션추천/매매일지] 저장 실패 — Excel 파일을 닫고 다시 실행하세요.")

    return results


def _safe(fn, code6, default):
    try:
        return fn(code6)
    except Exception:
        return default


def write_blocked_sheet(wb, results: list, now_str: str):
    SHEET = "⛔차단종목"
    if SHEET in wb.sheetnames:
        ws = wb[SHEET]; ws.delete_rows(1, ws.max_row)
    else:
        ws = wb.create_sheet(SHEET, 1)
    ws.cell(1, 1, f"⛔ 진입 차단 종목 — 점수와 무관하게 매수 금지   |   {now_str}").font = Font(name="맑은 고딕", bold=True, size=11)
    for c, h in enumerate(["종목명", "코드", "섹터", "현재가", "등락률%", "차단 사유"], 1):
        cell = ws.cell(3, c, h); cell.font = HDR_FONT; cell.fill = F_NAVY; cell.alignment = ALIGN_C
    r0 = 4
    for r in results:
        if r["grade"] != "⛔차단":
            continue
        for c, v in enumerate([r["name"], r["code"], r["sector"], r.get("price"),
                               round(r.get("change_rate") or 0, 2), " / ".join(r.get("blocks", []))], 1):
            cell = ws.cell(r0, c, v); cell.font = DATA_FONT; cell.fill = F_WARN
        r0 += 1
    for c, w in enumerate([16, 9, 12, 10, 9, 90], 1):
        ws.column_dimensions[chr(64 + c)].width = w


def _print_summary(results: list, regime: dict = None):
    if not results: return

    print(f"\n{'─'*68}")
    t_label, _ = current_time_strategy()
    reg_s = f"  |  레짐 {regime['label']}" if regime else ""
    print(f"  ▶ {t_label} 추천 종목 (v16 기준){reg_s}")
    print(f"{'─'*68}")

    valid = [r for r in results if r["grade"] not in ("⬇제외", "⛔차단")]
    valid.sort(key=lambda x: x["score"], reverse=True)

    print(f"  {'순위':4} {'종목명':16} {'점수':6} {'판정':8} {'가속':6} {'갭':6} {'등락':6} {'신호'}")
    print(f"  {'─'*4} {'─'*16} {'─'*6} {'─'*8} {'─'*6} {'─'*6} {'─'*6} {'─'*20}")

    for i, r in enumerate(valid[:10], 1):
        vol_s = f"{r['vol_accel']:.1f}x" if r['vol_accel'] else "  -  "
        gap_s = f"{r['gap_rate']:+.1f}%" if r['gap_rate'] else "  -  "
        chg_s = f"{r['change_rate']:+.1f}%" if r['change_rate'] else "  -  "
        sig   = r["signals"][0][:20] if r["signals"] else ""
        warn  = "⚠️" if r["warnings"] else ""
        print(f"  {i:4} {r['name']:16} {r['score']:6.1f} {r['grade']:8} {vol_s:6} {gap_s:6} {chg_s:6} {sig} {warn}")

    # 선도매수만 별도 강조
    lead = [r for r in valid if r["grade"] == "🚀선도매수"]
    if lead:
        print(f"\n  🔥 선도매수 후보 ({len(lead)}개):")
        for r in lead:
            sigs = " | ".join(r["signals"][:3])
            warns = " / ".join(r["warnings"][:2])
            print(f"     [{r['name']}]  점수={r['score']}  {sigs}")
            if warns: print(f"          ⚠️  {warns}")
    else:
        msg = "현재 선도매수 조건 충족 종목 없음"
        if regime and regime["label"] == "🔴단타비우호":
            msg += " (비우호 레짐 — 정상입니다. 무리하게 진입하지 마세요)"
        print(f"\n  ℹ️  {msg}")

    # v15: 차단 종목 (사유 표시)
    blocked = [r for r in results if r["grade"] == "⛔차단"]
    if blocked:
        print(f"\n  ⛔ 진입 차단 {len(blocked)}종목 (전체 목록은 엑셀 [⛔차단종목] 시트):")
        for r in blocked[:8]:
            print(f"     {r['name'][:14]:14}  {r['blocks'][0] if r.get('blocks') else ''}")

    print(f"{'─'*68}\n")


# ══════════════════════════════════════════════════════════════════
# 보유 종목 실시간 손익 추적
# ══════════════════════════════════════════════════════════════════
def track_holdings(holding_str: str, stop_pct: float = DEFAULT_STOP_PCT, profit_pct: float = DEFAULT_PROFIT_PCT):
    """--holding 삼성전자:75000,SK하이닉스:180000"""
    holdings = {}
    for item in holding_str.split(","):
        parts = item.strip().split(":")
        if len(parts) == 2:
            name, price_str = parts
            try: holdings[name.strip()] = int(price_str.strip().replace(",",""))
            except: pass

    if not holdings:
        print("[!] 보유 종목 형식 오류. 예: 삼성전자:75000,SK하이닉스:180000")
        return

    # 종목코드 매핑 (엑셀에서)
    xlsx = find_workbook()
    code_map = {}
    if xlsx:
        wb = openpyxl.load_workbook(xlsx, read_only=True)
        if "종목풀" in wb.sheetnames:
            ws = wb["종목풀"]
            for r in range(2, ws.max_row+1):
                n = ws.cell(r,1).value
                c = ws.cell(r,2).value
                if n and c:
                    try: code_map[str(n)] = f"{int(float(str(c))):06d}"
                    except: pass

    init_feeds()
    print(f"\n{'═'*55}")
    print(f"  📊 보유 종목 실시간 손익  [{datetime.now().strftime('%H:%M:%S')}]")
    print(f"{'═'*55}")
    print(f"  {'종목명':16} {'매수가':>10} {'현재가':>10} {'손익률':>8} {'손익액':>12}")
    print(f"  {'─'*16} {'─'*10} {'─'*10} {'─'*8} {'─'*12}")

    total_gain = 0
    for name, buy_price in holdings.items():
        code6 = code_map.get(name)
        if not code6:
            print(f"  {name:16} 코드 미확인 — 종목풀에 없는 종목")
            continue
        try:
            rt = fetch_realtime(code6)
            hist = [d for d in (fetch_daily(code6) or []) if d["date"] < datetime.now().strftime("%Y%m%d")]
        except KISError as e:
            print(f"  {name:16} 조회 실패 ({e})"); continue
        if not rt or not rt.get("price"):
            print(f"  {name:16} 조회 실패")
            continue
        cur = rt["price"]
        pnl_rate = (cur - buy_price) / buy_price * 100
        pnl_amt  = cur - buy_price
        icon = "▲" if pnl_rate >= 0 else "▼"
        color_hint = "✅" if pnl_rate >= 15 else ("⚠️" if pnl_rate <= -5 else "")
        print(f"  {name:16} {buy_price:>10,} {cur:>10,} {icon}{abs(pnl_rate):>6.1f}% {pnl_amt:>+12,} {color_hint}")

        # v13: 사용자 설정 손절/익절 기준 (기본 -5% / +8%)
        if pnl_rate >= profit_pct:
            print(f"    → ✅ 익절 목표 도달! +{profit_pct:.1f}% — 계획대로 매도")
        elif pnl_rate >= profit_pct * 0.7:
            print(f"    → 목표 근접 (+{profit_pct*0.7:.1f}%~) — 매도 준비")
        elif pnl_rate <= -stop_pct:
            print(f"    → 🔴 손절 기준 도달! -{stop_pct:.1f}% — 즉시 전량 손절 (버티기 금지)")
        elif pnl_rate <= -stop_pct * 0.7:
            print(f"    → ⚠️ 손절 근접 (-{stop_pct*0.7:.1f}%~) — 주의")

        # v15: 보유 중 위험 신호 (하락 추세 전환·악재 공시·투매·VWAP 이탈)
        sc = calc_score_v15(hist, rt, {}, False, None, _safe(fetch_filings, code6, []), None)
        flags = sc["blocks"] + [w for w in sc["warnings"] if ("음봉" in w or "VWAP" in w or "하락" in w)]
        flags = [f for f in flags if not f.startswith("갭업")]
        if flags:
            print(f"    ⚠️ 위험 신호: {' | '.join(flags[:4])}")

        time.sleep(REQUEST_DELAY)

    print(f"{'─'*55}\n")


# ══════════════════════════════════════════════════════════════════
# v15 신규: 매매일지 자동 채점 (--review)
# ══════════════════════════════════════════════════════════════════
# v14 --report 는 '결과'를 손으로 채워야 집계됐다 → 안 채우면 검증이 영영 안 됨.
# --review 는 추천 다음 거래일부터 REVIEW_HORIZON일 동안의 KIS 일봉으로
# '목표가 먼저 / 손절가 먼저'를 자동 판정해 [매매일지] 자동결과 칸에 쓴다.
def judge_trade(entry: float, stop: float, target: float, bars_after: list, horizon: int = REVIEW_HORIZON,
                day_close: Optional[float] = None) -> dict:
    """bars_after: 추천 다음 거래일부터 오래된 → 최신 순.
    day_close: 추천 당일 종가. v17 — 당일 오후에 손절가 아래로 마감한 추천이 '다음날부터' 채점돼
    성적이 실제보다 좋게 나오던 문제 보정 (당일 고저는 추천 시각 전후를 못 가려 종가만 쓴다)."""
    out = {"result": "진행중", "pnl": None, "mfe": 0.0, "mae": 0.0}
    if not entry or (not bars_after and not day_close):
        return out
    bars_after = bars_after or []
    if day_close:
        r0 = (day_close / entry - 1) * 100
        out["mfe"], out["mae"] = max(0.0, r0), min(0.0, r0)
        if day_close <= stop:
            out["result"], out["pnl"] = "손절", round(min(r0, (stop / entry - 1) * 100), 2)
        elif day_close >= target:
            out["result"], out["pnl"] = "익절", round((target / entry - 1) * 100, 2)
    for b in bars_after[:horizon]:
        out["mfe"] = max(out["mfe"], (b["high"] / entry - 1) * 100)
        out["mae"] = min(out["mae"], (b["low"] / entry - 1) * 100)
        if out["result"] != "진행중":
            continue
        hit_stop, hit_tgt = b["low"] <= stop, b["high"] >= target
        if hit_stop:                       # 같은 날 둘 다 닿으면 보수적으로 손절 처리
            out["result"], out["pnl"] = "손절", round((stop / entry - 1) * 100, 2)
        elif hit_tgt:
            out["result"], out["pnl"] = "익절", round((target / entry - 1) * 100, 2)
    if out["result"] == "진행중" and len(bars_after) >= horizon:
        last = bars_after[horizon - 1]["close"]
        out["result"], out["pnl"] = "기간만료", round((last / entry - 1) * 100, 2)
    out["mfe"], out["mae"] = round(out["mfe"], 2), round(out["mae"], 2)
    return out


def summarize_reviews(rows: list) -> list:
    """rows: [{"grade","result","pnl"}] → 출력용 문자열 목록."""
    lines = []
    done = [r for r in rows if r["result"] in ("익절", "손절", "기간만료")]
    if not done:
        return ["  확정된 추천이 아직 없습니다 (추천 후 5거래일이 지나야 판정)."]
    groups = {"전체": done}
    for r in done:
        groups.setdefault(r["grade"] or "-", []).append(r)
    for r in done:                                   # v17: 점수대·시간대별 — 어디서 돈을 잃는지 보이게
        s = r.get("score")
        if isinstance(s, (int, float)):
            groups.setdefault("점수 90↑" if s >= 90 else ("점수 75~89" if s >= 75 else "점수 75↓"), []).append(r)
    for r in done:
        t = str(r.get("time") or "")
        if len(t) >= 5:
            groups.setdefault("10시 이전" if t < "10:00" else ("10:00~11:30" if t < "11:30" else
                              ("11:30~14:10" if t < "14:10" else "14:10 이후")), []).append(r)
    for g, rs in groups.items():
        n = len(rs)
        win = sum(r["result"] == "익절" for r in rs)
        loss = sum(r["result"] == "손절" for r in rs)
        exp = sum(r["pnl"] for r in rs) / n - TOTAL_FRICTION_PCT
        lines.append(f"  {g:10} {n:3}건 | 익절 {win/n*100:4.0f}% | 손절 {loss/n*100:4.0f}% | "
                     f"거래당 기대값(비용·슬리피지 {TOTAL_FRICTION_PCT:.2f}% 차감) {exp:+.2f}%")
    return lines


def run_review(xlsx: Path):
    init_feeds()
    try:
        wb = openpyxl.load_workbook(xlsx)
    except PermissionError:
        print("[!] 파일이 Excel에서 열려 있습니다. 닫고 다시 실행하세요."); return
    if LOG_SHEET not in wb.sheetnames:
        print(f"[!] [{LOG_SHEET}] 시트가 없습니다. 스캔을 며칠 돌려 추천 기록부터 쌓으세요."); return
    ws = wb[LOG_SHEET]
    _ensure_log_header(ws)
    idx = {h: i + 1 for i, h in enumerate(LOG_HEADERS)}
    bars_cache, rows = {}, []
    for r in range(2, ws.max_row + 1):
        code = ws.cell(r, idx["코드"]).value
        day = ws.cell(r, idx["실행일자"]).value
        entry = to_float(ws.cell(r, idx["추천가"]).value)
        stop = to_float(ws.cell(r, idx["손절가"]).value)
        tgt = to_float(ws.cell(r, idx["목표가"]).value)
        if not code or not day or not entry or not stop or not tgt:
            continue
        code6 = norm_code(code)
        if code6 not in bars_cache:
            try:
                bars_cache[code6] = sorted(fetch_daily(code6) or [], key=lambda x: x["date"])
            except KISError:
                bars_cache[code6] = []
        d = str(day)[:10].replace("-", "")
        today_s = datetime.now().strftime("%Y%m%d")
        after = [b for b in bars_cache[code6] if b["date"] > d and b["date"] < today_s]
        day_bar = next((b for b in bars_cache[code6] if b["date"] == d and d < today_s), None)
        j = judge_trade(entry, stop, tgt, after, day_close=day_bar["close"] if day_bar else None)
        ws.cell(r, idx["자동결과"], j["result"])
        ws.cell(r, idx["자동손익률(%)"], j["pnl"])
        ws.cell(r, idx["최대상승(%)"], j["mfe"])
        ws.cell(r, idx["최대하락(%)"], j["mae"])
        rows.append({"grade": ws.cell(r, idx["등급"]).value, "score": ws.cell(r, idx["점수"]).value,
                     "time": ws.cell(r, idx["실행시각"]).value, **j})
    try:
        wb.save(xlsx)
    except PermissionError:
        print("  ✗ 저장 실패 — Excel 파일을 닫고 다시 실행하세요.")
    print(f"\n{'═'*68}\n  📊 추천 자동 채점 (추천가 진입 가정, 다음날부터 {REVIEW_HORIZON}거래일)\n{'═'*68}")
    for line in summarize_reviews(rows):
        print(line)
    print("  ※ 20건 이상 쌓여야 의미 있음. 기대값이 마이너스인 등급·점수대·시간대는 매수 중단.")
    print(f"  상세는 [{LOG_SHEET}] 시트의 자동결과·자동손익률 칸.\n")


# ══════════════════════════════════════════════════════════════════
# v15 신규: 연결·종목코드 점검 (--check)
# ══════════════════════════════════════════════════════════════════
def run_check(xlsx: Path):
    init_feeds()
    rt = fetch_realtime("005930")
    print(f"  삼성전자 현재가 {rt['price']:,}원 ({rt['change_rate']:+.2f}%)  VWAP {rt['vwap'] or 0:,.0f}  시총 {rt['_mcap']:,}억")
    d = fetch_daily("005930")
    print(f"  일봉 {len(d)}개 수신 (최근 {d[0]['date']})")
    inv = _safe(fetch_investor, "005930", {})
    print(f"  투자자 동향: {'OK' if inv else '실패'}   체결강도: {_safe(fetch_strength, '005930', None)}")
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb["종목풀"]
    bad = n = 0
    seen = {}
    print("\n  종목풀 종목명·코드 대조 중...")
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0] or not row[1]:
            continue
        try: code6 = f"{int(float(str(row[1]))):06d}"
        except: print(f"  [!] {row[0]}: 코드 형식 오류 {row[1]}"); bad += 1; continue
        n += 1
        if code6 in seen:
            print(f"  [!] 코드 중복 {code6}: {seen[code6]} / {row[0]}"); bad += 1; continue
        seen[code6] = row[0]
        real = _safe(fetch_name, code6, "")
        a, b = str(row[0]).replace(" ", ""), real.replace(" ", "")
        if not b or (a not in b and b not in a):
            print(f"  [!] {row[0]} ({code6}) → 실제 종목명: {real or '조회 안 됨'}"); bad += 1
    print(f"  점검 {n}종목, 문제 {bad}건{' — 종목풀 시트에서 고치세요' if bad else ' — 이상 없음'}\n")


# ══════════════════════════════════════════════════════════════════
# 실시간 알림 모드 (선도 진입 신호)
# ══════════════════════════════════════════════════════════════════
def alert_mode(xlsx: Path, interval_min: int = 3,
                capital: float = DEFAULT_CAPITAL, max_pos: int = DEFAULT_MAX_POS,
                stop_pct: float = DEFAULT_STOP_PCT, profit_pct: float = DEFAULT_PROFIT_PCT):
    """선도 진입 신호 발생 시 즉시 출력"""
    print(f"\n★ 알림 모드: {interval_min}분마다 선도매수 신호 스캔 (Ctrl+C 종료)\n")
    alerted = set()

    while True:
        mst = market_status()
        if mst != "OPEN":
            if mst == "AFTER":
                print("  장 마감. 알림 모드 종료."); break
            time.sleep(60); continue

        results = run_update(xlsx, verbose=False, capital=capital, max_pos=max_pos, stop_pct=stop_pct, profit_pct=profit_pct)
        t_label, _ = current_time_strategy()
        if "❌" in t_label or "⏸" in t_label:
            leads = []          # 진입 금지/소강 시간대엔 알림 안 함 (v10 시간대 원칙)
        else:
            leads = [r for r in results if r["grade"] == "🚀선도매수"]

        new_leads = [r for r in leads if r["name"] not in alerted]
        if new_leads:
            print(f"\n{'🔔'*20}")
            print(f"  ⚡ 선도매수 신호! [{datetime.now().strftime('%H:%M:%S')}]")
            for r in new_leads:
                print(f"  ★ {r['name']}  점수={r['score']}  {' | '.join(r['signals'][:2])}")
                alerted.add(r["name"])
            print(f"{'🔔'*20}\n")

        next_t = datetime.now() + timedelta(minutes=interval_min)
        print(f"  다음 스캔: {next_t:%H:%M} (선도매수 신호 있으면 즉시 알림)")
        time.sleep(interval_min * 60)


# ══════════════════════════════════════════════════════════════════
# 자체 테스트 (--selftest) — API 없이 채점·판정 로직 검증
# ══════════════════════════════════════════════════════════════════
def _mk_hist(closes_old_to_new, vol=1_000_000):
    d0 = datetime(2026, 6, 1)
    rows = []
    for i, c in enumerate(closes_old_to_new):
        c = int(c)
        rows.append({"date": (d0 + timedelta(days=i)).strftime("%Y%m%d"), "open": int(c * (0.99 if i % 2 else 1.01)),
                     "high": int(c * 1.02), "low": int(c * 0.98), "close": c, "volume": vol, "amount": c * vol})
    return list(reversed(rows))          # 최신 → 과거 (fetch_daily 형식)


def selftest() -> int:
    global _NOW_OVERRIDE
    _NOW_OVERRIDE = datetime(2026, 9, 24, 14, 20)
    fails = 0

    def check(title, cond):
        nonlocal fails
        print(f"  {'✅' if cond else '❌'} {title}")
        fails += 0 if cond else 1

    # A. 휴림로봇형: 3개월 하락 추세 + 거래량 실린 음봉 + 유상증자 공시
    hist = _mk_hist([10900 - i * 70 for i in range(70)], vol=1_200_000)
    rt = dict(price=5900, change_rate=-2.8, today_volume=1_600_000, today_open=6150, today_high=6200,
              today_low=5850, prev_close=6070, vwap=6010, _mcap=7000, _pgtr_ntby=-30000, _frgn_ntby=-50000,
              stat_code="55", warn_code="00")
    sc = calc_score_v15(hist, rt, {"inst_net": -1, "inst_3d": -3, "fore_3d": -3}, False, 85,
                        ["20260923 유상증자결정(일반공모)"], -0.5)
    check(f"A 휴림형(하락추세+투매+유증) → 차단: {sc['grade']} {sc['blocks']}", sc["grade"] == "⛔차단")
    check(f"A 거래량 실린 음봉 감점: ①={sc['bd'].get('①거래량가속')}", sc["bd"].get("①거래량가속", 0) < 0)
    sc2 = calc_score_v15(hist, rt, {}, False, None, [], None)
    check(f"A' 공시 정보 없어도 추세 게이트로 차단: {sc2['blocks']}", sc2["grade"] == "⛔차단")

    # B. 상승 추세 속 눌림 후 반등 + 거래량 2배 + 수급
    closes = [10000 + i * 60 for i in range(65)] + [14100, 14000, 13700, 13500, 13400]
    hist = _mk_hist(closes, vol=800_000)
    frac = expected_volume_fraction(elapsed_market_minutes())
    rt = dict(price=13800, change_rate=2.99, today_volume=int(800_000 * frac * 2.2), today_open=13450,
              today_high=13850, today_low=13400, prev_close=13400, vwap=13620, _mcap=3000,
              _pgtr_ntby=15000, _frgn_ntby=20000, stat_code="55", warn_code="00")
    sc = calc_score_v15(hist, rt, {"inst_net": 5, "inst_3d": 10, "fore_3d": 10}, False, 125, [], 1.2)
    check(f"B 눌림 반등형 → 선도매수: {sc['grade']} {sc['total']}점 {sc['bd']}", sc["grade"] == "🚀선도매수")

    # C. 갭업 5% → 차단
    rt_c = dict(rt, today_open=int(13400 * 1.05), today_high=14200, today_low=14000, price=14100)
    sc = calc_score_v15(hist, rt_c, {}, False, None, [], None)
    check(f"C 갭업 +5% → 차단: {sc['blocks']}", sc["grade"] == "⛔차단" and any("갭업" in b for b in sc["blocks"]))

    # D. v14 네이버 버그형 불가능 값 → 채점 거부
    sc = calc_score_v15(hist, dict(rt, today_low=15000), {}, False, None, [], None)
    check(f"D 저가>현재가 같은 이상값 → 차단: {sc['blocks']}", sc["grade"] == "⛔차단")

    # E. VWAP 아래면 선도매수 불가
    sc = calc_score_v15(hist, dict(rt, vwap=13900), {"inst_net": 5, "inst_3d": 10, "fore_3d": 10}, False, 125, [], 1.2)
    check(f"E VWAP 아래 → 선도매수 아님: {sc['grade']}", sc["grade"] != "🚀선도매수")

    # F. 레짐 라벨: 비우호 장에서 모멘텀 모드가 켜지면 안 됨 (v14 버그)
    lab = "🔴단타비우호"
    check("F 비우호 레짐 → 모멘텀 모드 OFF", not ("우호" in lab and "비우호" not in lab))

    # G. 자동 채점
    bars = [{"date": "20260925", "high": 10300, "low": 9800, "close": 10100},
            {"date": "20260926", "high": 10900, "low": 10000, "close": 10800}]
    j = judge_trade(10000, 9500, 10800, bars)
    check(f"G 이틀째 목표가 도달 → 익절 {j}", j["result"] == "익절" and j["pnl"] == 8.0)
    j = judge_trade(10000, 9500, 10800, [{"date": "20260925", "high": 10900, "low": 9400, "close": 9600}])
    check(f"G 같은 날 목표·손절 동시 → 보수적 손절 {j['result']}", j["result"] == "손절")

    # H. v16: 호가단위·ATR 계획·위험 기준 수량·코드·임계값 합산
    check("H 호가단위: 6,480→손절 내림 6,480 / 12,345→12,340 / 51,230→51,200",
          round_tick(6480, False) == 6480 and round_tick(12345, False) == 12340 and round_tick(51230, False) == 51200)
    hist = _mk_hist([10000] * 30)          # 매일 고저폭 4% → ATR ≈ 400 (4%)
    pl = trade_plan(10000, hist, 5.0)
    check(f"H ATR 4% → 손절폭 min(1.5×4=6%, 상한5%) = 5%, 목표 +10%: {pl}",
          pl["stop"] == 9500 and pl["target"] == 11000 and pl["basis"] == "ATR")
    pl2 = trade_plan(10000, _mk_hist([10000 + (i % 2) for i in range(30)]), 5.0)
    check(f"H 변동성 작아도 손절폭 하한 2%: {pl2['stop_pct']}%", pl2["stop_pct"] >= 2.0)
    pos = size_position({"price": 10000, "plan": pl}, 9_000_000, 3, 5, 8)
    check(f"H 종목당 손실 ≤ 자본 1%(9만원): 수량 {pos['shares']} 최대손실 {pos['max_loss']:,.0f}",
          pos["max_loss"] <= 90_000 and pos["shares"] == 180)
    check("H 신규 영문 코드 0015G0 허용 / 90710→090710",
          norm_code("0015G0") == "0015G0" and norm_code(90710) == "090710" and norm_code("ABC") is None)
    th = combine_thresholds({"thresholds": {k: v + 20 for k, v in GRADE.items()}}, {"extra": 15})
    check(f"H 레짐+20 & 지수+15 → 합산 상한 +{MAX_EXTRA_THRESHOLD}: 선도 {th['선도매수']}",
          th["선도매수"] == GRADE["선도매수"] + MAX_EXTRA_THRESHOLD)

    # I. v16.1 선도매수 자격·정원 (10/2 스캔에서 28개 선도매수가 나온 문제)
    closes = [10000 + i * 60 for i in range(65)] + [14100, 14000, 13700, 13500, 13400]
    hist = _mk_hist(closes, vol=800_000)
    frac = expected_volume_fraction(elapsed_market_minutes())
    good = dict(price=13800, change_rate=2.99, today_volume=int(800_000 * frac * 2.2), today_open=13450,
                today_high=13850, today_low=13400, prev_close=13400, vwap=13620, _mcap=3000,
                _pgtr_ntby=15000, _frgn_ntby=20000, stat_code="55", warn_code="00")
    inv = {"inst_net": 5, "inst_3d": 10, "fore_3d": 10}
    sc = finalize_grade(calc_score_v15(hist, good, inv, False, 69, [], 1.2), GRADE)
    check(f"I 점수 높아도 체결강도 69 → 관심까지만: {sc['grade']} {sc['lead_block']}", sc["grade"] == "👀관심")
    hot = dict(good, price=14790, change_rate=10.4, today_high=14800, vwap=14300)
    sc = finalize_grade(calc_score_v15(hist, hot, inv, False, 150, [], 1.2), GRADE)
    check(f"I 당일 +10% → 선도 불가: {sc['grade']} {sc['lead_block']}", sc["grade"] != "🚀선도매수")
    res = [{"grade": "🚀선도매수", "score": 100 - i} for i in range(28)]
    n = cap_lead_count(res, 6)
    check(f"I 선도 28개 → 상위 6개만 유지: {n}, 남은 선도 {sum(r['grade'] == '🚀선도매수' for r in res)}",
          n == 6 and sum(r["grade"] == "🚀선도매수" for r in res) == 6 and res[0]["grade"] == "🚀선도매수")

    # J. v17: 급등 말기 차단 · 이격 감점 · 변동성 · 지수 게이트 · 분산 · 당일 종가 채점
    run = [10000 + i * 20 for i in range(60)] + [11300, 11500, 11800, 12200, 12700, 13300, 14000, 14800, 15600, 16500]
    hist_run = _mk_hist(run, vol=800_000)
    hot2 = dict(price=17000, change_rate=3.0, today_volume=int(800_000 * frac * 2.5), today_open=16600,
                today_high=17050, today_low=16550, prev_close=16500, vwap=16800, _mcap=3000,
                _pgtr_ntby=15000, _frgn_ntby=20000, stat_code="55", warn_code="00")
    sc = calc_score_v15(hist_run, hot2, inv, True, 130, [], 1.2)
    check(f"J 10일 급등(이격 {sc.get('disparity')}%) 종목은 모멘텀 모드라도 차단: {sc['blocks']}",
          sc["grade"] == "⛔차단" and any("이격" in b or "급등 말기" in b for b in sc["blocks"]))
    mid = [10000 + i * 40 for i in range(62)] + [12550, 12650, 12800, 12950, 13100, 13300, 13500, 13700]
    hist_mid = _mk_hist(mid, vol=800_000)
    m2 = dict(hot2, price=14100, today_open=13750, today_high=14150, today_low=13700, prev_close=13700,
              change_rate=2.9, vwap=13900)
    sc = finalize_grade(calc_score_v15(hist_mid, m2, inv, True, 130, [], 1.2), GRADE)
    check(f"J 이격 {sc.get('disparity')}% → 감점 + 선도·대기 불가: {sc['grade']} {sc.get('lead_block')}",
          sc["grade"] == "👀관심" and "20일선 이격 과다" in sc["lead_block"] and sc["bd"]["⑥과열페널티"] <= -10)
    wild = _mk_hist(closes, vol=800_000)
    for d in wild:
        d["high"], d["low"] = int(d["close"] * 1.045), int(d["close"] * 0.955)      # 하루 변동폭 9%
    sc = finalize_grade(calc_score_v15(wild, good, inv, False, 125, [], 1.2), GRADE)
    check(f"J ATR {sc.get('atr_pct')}% > 손절상한 → 진입 등급 불가: {sc['grade']}",
          sc["grade"] == "👀관심" and any("변동성" in w for w in sc["lead_block"]))
    sc = calc_score_v15(hist, dict(good, _pgtr_ntby=-60000), inv, False, 125, [], 1.2)
    check("J 프로그램 -6만주(8억원) → 금액 기준 대량 순매도", any("대량 순매도" in w for w in sc["warnings"]))
    a = [1.0, -2.0, 3.0, 0.5, -1.0, 2.0, -0.5, 1.5, -2.5, 0.8, 1.1, -0.3]
    cands = [{"name": "A", "sector": "기계·장비", "rets20": a}, {"name": "B", "sector": "전기·전자", "rets20": [x * 1.1 for x in a]},
             {"name": "C", "sector": "기계·장비", "rets20": a[::-1]}, {"name": "D", "sector": "화학", "rets20": [-x for x in a]}]
    pk = [r["name"] for r in pick_diversified(cands, 3)]
    check(f"J 업종이 달라도 같이 움직이면 제외 / 섹터당 1개: {pk}", pk == ["A", "D"])
    j = judge_trade(10000, 9500, 11000, [{"date": "20260925", "high": 11200, "low": 9900, "close": 11100}], day_close=9300)
    check(f"J 추천 당일 종가가 손절가 아래 → 다음날 올라도 손절: {j}", j["result"] == "손절" and j["pnl"] == -7.0)
    old_fd = fetch_daily
    try:
        globals()["fetch_daily"] = lambda code: [{"close": c} for c in ([9000] + [10000] * 25)]
        g = assess_market_gate()
    finally:
        globals()["fetch_daily"] = old_fd
    check(f"J 지수 ETF 둘 다 20일선 아래 → 신규 진입 중단: {g['desc'][:60]}", g["block"] is True)
    check("J 점심 소강·개장 30분·장외는 진입 창 아님",
          not entry_window_ok("⏸소강구간") and not entry_window_ok("❌진입금지") and entry_window_ok("✅최적단타"))

    _NOW_OVERRIDE = None
    print(f"\n  자체 테스트 {'통과' if fails == 0 else f'실패 {fails}건'}")
    return fails


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="단타 스캐너 v16 (KIS)")
    parser.add_argument("--watch",   type=int,  metavar="N",  help="N분마다 반복 실행")
    parser.add_argument("--alert",   action="store_true",      help="선도 신호 알림 모드")
    parser.add_argument("--holding", type=str,  metavar="종목:매수가,...", help="보유 종목 손익 추적")
    parser.add_argument("--once",    action="store_true",      help="1회 실행 후 종료")
    parser.add_argument("--capital",   type=float, default=DEFAULT_CAPITAL,   metavar="원",  help="총 투입 자금 (기본 10,000,000)")
    parser.add_argument("--positions", type=int,   default=DEFAULT_MAX_POS,   metavar="N",   help="동시 보유 목표 종목 수 (기본 5)")
    parser.add_argument("--stop",      type=float, default=DEFAULT_STOP_PCT,  metavar="%",  help="손절 기준 %% (기본 5)")
    parser.add_argument("--profit",    type=float, default=DEFAULT_PROFIT_PCT,metavar="%",  help="익절 기준 %% (기본 8)")
    parser.add_argument("--report",    action="store_true",       help="[매매일지] 수기 결과 집계 리포트")
    parser.add_argument("--review",    action="store_true",       help="[매매일지] 추천 자동 채점 (v15)")
    parser.add_argument("--check",     action="store_true",       help="KIS 연결·종목코드 점검 (v15)")
    parser.add_argument("--selftest",  action="store_true",       help="API 없이 채점 로직 자체 테스트")
    args = parser.parse_args()

    if args.selftest:
        return 1 if selftest() else 0
    if args.review or args.check:
        xlsx = find_workbook()
        if xlsx is None:
            input("\nEnter 키를 눌러 종료..."); return 1
        try:
            run_review(xlsx) if args.review else run_check(xlsx)
        except KISError as e:
            print(f"[!] {e}"); return 1
        return 0

    # 매매일지 리포트 모드
    if args.report:
        xlsx = find_workbook()
        if xlsx is None:
            input("\nEnter 키를 눌러 종료...")
            return 1
        print_trade_report(xlsx)
        return 0

    # 보유 종목 추적 모드
    if args.holding:
        try:
            track_holdings(args.holding, stop_pct=args.stop, profit_pct=args.profit)
        except KISError as e:
            print(f"[!] {e}"); return 1
        return 0

    xlsx = find_workbook()
    if xlsx is None:
        input("\nEnter 키를 눌러 종료...")
        return 1

    print(f"[i] 파일: {xlsx.name}")

    rp = dict(capital=args.capital, max_pos=args.positions, stop_pct=args.stop, profit_pct=args.profit)

    try:
        init_feeds()
    except KISError as e:
        print(f"[!] {e}")
        input("\nEnter 키를 눌러 종료..."); return 1

    try:
        if args.alert:
            alert_mode(xlsx, **rp)
        elif args.watch:
            print(f"★ 스케줄러: {args.watch}분마다 업데이트 (Ctrl+C 종료)\n")
            while True:
                mst = market_status()
                if mst == "OPEN":
                    run_update(xlsx, **rp)
                    next_t = datetime.now() + timedelta(minutes=args.watch)
                    print(f"  다음 업데이트: {args.watch}분 후 ({next_t:%H:%M})")
                    time.sleep(args.watch * 60)
                elif mst == "BEFORE":
                    opens = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
                    wait  = max(0, (opens - datetime.now()).total_seconds())
                    print(f"  장 시작 전 대기 ({int(wait//60)}분 후 개장)")
                    time.sleep(min(wait+5, 60))
                else:
                    print("  장 마감. 종료."); break
        else:
            run_update(xlsx, **rp)
            if not args.once:
                print("  3초 후 종료..."); time.sleep(3)
    except KeyboardInterrupt:
        print("\n[중단됨]")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n[!] 오류: {e}")
        import traceback; traceback.print_exc()
        input("\nEnter 키를 눌러 종료...")
        sys.exit(1)
