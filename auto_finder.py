# -*- coding: utf-8 -*-
"""
auto_finder.py — 대장주 자동 발굴기

조건 3가지를 모두 만족하는 종목을 시장에서 자동으로 찾아 올려준다:
  ① 대형주 (시가총액 상위 / 하한 이상)
  ② 상승추세 (현재가 > 200일 이동평균)
  ③ 눌림 (최근 20거래일 고점 대비 -X% 이상)

※ 추천이 아니라 '조건 스크리닝'이다. 매수 여부·종목·금액은 본인 판단.
※ 대형주만 대상. 초소형 급등락은 이 도구 범위 밖.

준비: pip install finance-datareader requests   (kis_feed.py 도 같은 폴더에)
사용:
  CLI  : python auto_finder.py
  GUI  : python gui.py
  코드에서: run_scan(...) 을 호출 (gui.py 참고)
"""
from __future__ import annotations
from datetime import datetime, timedelta

import kis_feed as kf   # _get, _throttle, _i, fetch_realtime, _load_keys 재사용

# ── 발굴 조건 기본값 (GUI/run_scan 에서 override 가능) ──────────────
TOP_N        = 80       # 시총 상위 몇 개를 훑을지 (많을수록 느림)
MIN_MCAP_억  = 20_000   # 최소 시가총액(억원). 2조 미만 제외 = 진짜 대형주만
PULLBACK_PCT = 5.0      # 눌림 기준(%). 20일 고점 대비
LOOKBACK     = 20       # 고점 산정 거래일
MA_PERIOD    = 200      # 추세 판단 이동평균 (현재가 > 200일선 = 상승추세)
MAX_SHOW     = 25       # 결과 최대 표시 개수
MIN_AVG_TRADE_VALUE_억 = 30   # 최소 평균거래대금(억원). LOOKBACK일 평균, 유동성 없는 종목 제외
MAX_PER      = 0        # PER 상한 (0 = 필터 안 함). 적자 등 PER 없는 종목은 필터 대상에서 제외하지 않음
MAX_PBR      = 0        # PBR 상한 (0 = 필터 안 함)


def _fetch_history(code6: str, min_rows: int = 210):
    """
    200일선 계산용 장기 일봉. KIS 일봉은 1회 ~100개라 날짜창을 뒤로 밀며 모은다.
    반환: 최신순 [{date, high, low, close, volume}, ...]  (실패 시 None)
    """
    collected = {}
    end = datetime.now()
    for _ in range(5):  # 최대 5회 (~450 영업일)
        start = end - timedelta(days=130)
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
        if not arr:
            break
        dates = []
        for it in arr:
            d = it.get("stck_bsop_date")
            c = kf._i(it.get("stck_clpr"))
            if not d or c is None:
                continue
            collected[d] = {
                "date": d,
                "high":   kf._i(it.get("stck_hgpr"), 0),
                "low":    kf._i(it.get("stck_lwpr"), 0),
                "close":  c,
                "volume": kf._i(it.get("acml_vol"), 0),
            }
            dates.append(d)
        if len(collected) >= min_rows or not dates:
            break
        end = datetime.strptime(min(dates), "%Y%m%d") - timedelta(days=1)
        kf._throttle()

    if len(collected) < LOOKBACK:
        return None
    return sorted(collected.values(), key=lambda x: x["date"], reverse=True)


def load_top_marcap(top_n: int, min_mcap_억: int, log_cb=None):
    """KRX 시총 상위 종목. 반환: [(code6, name, mcap억), ...]"""
    def log(msg):
        if log_cb:
            log_cb(msg)

    import pandas as pd
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX-MARCAP")
    cols = {c.lower(): c for c in df.columns}
    code_col = cols.get("code") or cols.get("symbol")
    name_col = cols.get("name")
    mcap_col = cols.get("marcap") or cols.get("markcap") or cols.get("marketcap")
    if not code_col or not name_col:
        raise RuntimeError(f"컬럼 확인 필요: {list(df.columns)}")

    # fdr.StockListing('KRX-MARCAP')은 기본적으로 GitHub에 캐시된 스냅샷 CSV를 쓰는데,
    # 이 캐시가 당일 데이터를 아직 못 채운 경우 Close/Marcap이 전부 비어 있는 채로 온다.
    # KRX 서버 직접 조회는 방화벽/사내망 등에서 막히는 경우가 많아 불안정하므로,
    # 대신 GitHub 캐시에서 하루씩 이전 영업일로 거슬러 올라가며 데이터가 채워진
    # 날짜를 찾는다(전 영업일 마감 기준 시총으로도 '대형주' 판별에는 충분).
    if not mcap_col or df[mcap_col].notna().mean() < 0.5:
        found = False
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
            c_mcap_col = c_cols.get("marcap") or c_cols.get("markcap") or c_cols.get("marketcap")
            if c_mcap_col and candidate[c_mcap_col].notna().mean() >= 0.5:
                df = candidate
                cols = c_cols
                code_col = cols.get("code") or cols.get("symbol")
                name_col = cols.get("name")
                mcap_col = c_mcap_col
                log(f"[참고] 당일 시총 데이터가 비어있어 {date_str}(전 영업일) 마감 기준으로 대체합니다.")
                found = True
                break
            d -= timedelta(days=1)

        if not found:
            raise RuntimeError(
                "최근 10일 내 시총 데이터를 찾지 못했습니다. "
                "FinanceDataReader/KRX 쪽 문제일 수 있으니 잠시 후 다시 시도하세요."
            )

    if not mcap_col:
        raise RuntimeError(
            f"시가총액 컬럼을 찾을 수 없습니다 (컬럼 목록: {list(df.columns)}). "
            "FinanceDataReader 데이터 소스가 바뀌었을 수 있습니다."
        )

    # 시총 데이터가 결측(NaN/0)이면 정렬 자체가 의미 없어지고 '대형주 상위 N개'가
    # 실제로는 무작위 종목이 되어버린다 — 조용히 넘어가지 않고 바로 알린다.
    valid_ratio = df[mcap_col].notna().mean() if len(df) else 0
    if valid_ratio < 0.5:
        raise RuntimeError(
            f"KRX 시총 데이터 결측이 심합니다(정상비율 {valid_ratio:.0%}). "
            "FinanceDataReader/KRX 쪽 일시적 데이터 문제일 수 있으니 잠시 후 다시 시도하세요."
        )

    df = df.sort_values(mcap_col, ascending=False)

    out = []
    skipped_no_mcap = 0
    for _, row in df.iterrows():
        code = str(row[code_col]).zfill(6)
        name = str(row[name_col])
        mcap_won = row[mcap_col]
        try:
            mcap_억 = int(mcap_won / 1e8) if mcap_won and mcap_won == mcap_won else None
        except Exception:
            mcap_억 = None
        # ETF·우선주·스팩·리츠 등 제외
        if any(k in name for k in ["우B", "우선", "스팩", "리츠", "TIGER", "KODEX",
                                    "ETF", "ACE", "PLUS", "RISE", "SOL "]):
            continue
        if name.endswith("우"):   # 우선주
            continue
        if mcap_억 is None:
            skipped_no_mcap += 1
            continue  # 시총 정보 없는 종목은 '대형주' 조건을 확인할 수 없으므로 제외
        if mcap_억 < min_mcap_억:
            continue
        out.append((code, name, mcap_억))
        if len(out) >= top_n:
            break

    if skipped_no_mcap:
        log(f"[참고] 시총 정보 없어 제외된 종목 {skipped_no_mcap}개")
    return out


def run_scan(
    top_n: int = TOP_N,
    min_mcap_억: int = MIN_MCAP_억,
    pullback_pct: float = PULLBACK_PCT,
    lookback: int = LOOKBACK,
    ma_period: int = MA_PERIOD,
    min_avg_trade_value_억: float = MIN_AVG_TRADE_VALUE_억,
    max_per: float = MAX_PER,
    max_pbr: float = MAX_PBR,
    progress_cb=None,
    stop_flag=None,
    log_cb=None,
):
    """
    스캔을 실행하고 (hits, knives, scanned, universe_len) 을 반환한다.

    progress_cb(done, total, code, name) : 종목 1개 처리할 때마다 호출 (GUI 진행바용)
    stop_flag() -> bool                  : True 를 반환하면 중단
    log_cb(str)                          : 로그 메시지 전달

    거래대금(유동성) 조건: 최근 lookback일 평균거래대금이 min_avg_trade_value_억
    미만이면 제외한다. 매매가 뜸한 종목(허수 눌림)을 걸러내기 위함.

    밸류에이션 조건: max_per / max_pbr 을 0보다 크게 주면 그 값을 초과하는
    종목을 제외한다(둘 다 0이면 필터 안 함). PER/PBR이 없는 종목(적자 등)은
    필터로 거르지 않고 통과시킨 뒤 결과에 None으로 표시한다 — 필터링해도
    적자라서가 아니라 '값이 없어서' 빠지는 걸 방지하기 위함.

    hits / knives 원소:
      (name, code, mcap억, price, pullback%, above_ma%, avg_trade_value억, vol_ratio, per, pbr)
      vol_ratio = 최근 거래일 거래량 / lookback일 평균거래량 (1.0 = 평균과 동일)
      per / pbr 은 float 또는 None(데이터 없음)
    """
    def log(msg):
        if log_cb:
            log_cb(msg)

    global LOOKBACK
    LOOKBACK = lookback  # _fetch_history 가 참조

    kf._load_keys()
    log(f"KRX 시총 상위 종목 로딩 중... (상위 {top_n}개, {min_mcap_억:,}억 이상)")
    universe = load_top_marcap(top_n, min_mcap_억, log_cb=log)
    log(f"대상 대형주: {len(universe)}개")

    hits, knives, scanned = [], [], 0
    total = len(universe)

    for idx, (code, name, mcap) in enumerate(universe, start=1):
        if stop_flag and stop_flag():
            log("사용자 중단 요청 — 스캔을 멈춥니다.")
            break

        daily = None
        try:
            daily = _fetch_history(code, ma_period + 10)
        except Exception as e:
            log(f"  [경고] {name}({code}) 시세 조회 실패: {e}")

        if progress_cb:
            progress_cb(idx, total, code, name)

        if not daily or len(daily) < lookback:
            continue
        scanned += 1

        window = daily[:lookback]
        high_n = max(d["high"] for d in window)

        closes = [d["close"] for d in daily]
        ma_n = min(ma_period, len(closes))
        ma_val = sum(closes[:ma_n]) / ma_n

        volumes = [d["volume"] for d in window]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        avg_trade_value_억 = (
            sum(d["volume"] * d["close"] for d in window) / len(window) / 1e8 if window else 0.0
        )
        today_vol = daily[0]["volume"]
        vol_ratio = (today_vol / avg_vol) if avg_vol else 0.0

        if avg_trade_value_억 < min_avg_trade_value_억:
            continue  # 유동성 부족 — 매매가 뜸한 종목은 제외

        rt = None
        try:
            rt = kf.fetch_realtime(code)
        except Exception:
            pass
        price = (rt or {}).get("price") or daily[0]["close"]
        per = (rt or {}).get("per")
        pbr = (rt or {}).get("pbr")

        if max_per and per is not None and per > max_per:
            continue
        if max_pbr and pbr is not None and pbr > max_pbr:
            continue

        pullback = (high_n - price) / high_n * 100 if high_n else 0.0
        uptrend = price > ma_val
        above_ma = (price - ma_val) / ma_val * 100 if ma_val else 0.0

        if pullback >= pullback_pct:
            rec = (name, code, mcap, price, pullback, above_ma, avg_trade_value_억, vol_ratio, per, pbr)
            (hits if uptrend else knives).append(rec)

    hits.sort(key=lambda x: x[4], reverse=True)
    knives.sort(key=lambda x: x[4], reverse=True)
    log(f"스캔 완료: {scanned}개 조회 / 충족 {len(hits)}개 / 칼날주의 {len(knives)}개")
    return hits, knives, scanned, len(universe)


def main():
    print("=" * 74)
    print(f"  대장주 자동 발굴  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]")
    print(f"  조건: 시총상위 {TOP_N}개(≥{MIN_MCAP_억:,}억) + 200일선 위 + {LOOKBACK}일고점 대비 -{PULLBACK_PCT:.0f}% + 평균거래대금 ≥{MIN_AVG_TRADE_VALUE_억}억")
    print("=" * 74)

    try:
        kf._load_keys()
    except Exception as e:
        print(f"[!] KIS 키가 없어 실행할 수 없습니다: {e}")
        return

    try:
        hits, knives, scanned, uni_len = run_scan(log_cb=print)
    except ImportError:
        print("[!] FinanceDataReader 없음. 설치:  pip install finance-datareader")
        return
    except Exception as e:
        print(f"[!] 스캔 실패: {e}")
        return

    print(f"\n[i] 스캔 완료: {scanned}개 조회 (대상 {uni_len}개)")
    print("=" * 74)
    print(f"  🟢 조건 충족 — 대형주 + 상승추세(200일선 위) + 눌림 ({len(hits)}개)")
    print("=" * 74)
    if hits:
        print(f"  {'종목명':16} {'코드':>7} {'시총(억)':>9} {'현재가':>10} {'눌림':>7} {'200일선위':>9} {'평균거래대금':>10} {'거래량비':>7} {'PER':>7} {'PBR':>6}")
        print("  " + "-" * 108)
        for name, code, mcap, price, pb, above, tv, vr, per, pbr in hits[:MAX_SHOW]:
            mc = f"{mcap:,}" if mcap else "-"
            per_s = f"{per:.1f}" if per is not None else "-"
            pbr_s = f"{pbr:.2f}" if pbr is not None else "-"
            print(f"  {name:16} {code:>7} {mc:>9} {price:>10,} {pb:>6.1f}% {above:>7.1f}% {tv:>9,.0f}억 {vr:>6.1f}x {per_s:>7} {pbr_s:>6}")
        print(f"\n  ※ '눌림' 클수록 고점서 많이 빠짐 / '200일선위' 클수록 추세 강함.")
        print(f"  ※ 조건 스크리닝일 뿐. 매수 여부·종목·금액은 본인 판단.")
    else:
        print("  조건 충족 종목 없음 (지금 대형주 대부분 200일선 아래거나 눌림 부족).")

    if knives:
        print("\n" + "-" * 74)
        print(f"  ⚠️ 참고: 눌림은 왔지만 하락추세(200일선 아래) — '칼날' 주의 ({len(knives)}개)")
        print("  " + ", ".join(k[0] for k in knives[:15]))
    print("=" * 74)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        print("\n[오류 발생]")
        traceback.print_exc()
    print()
    input("=== 끝났습니다. 엔터 키를 누르면 창이 닫힙니다. ===")
