# 인수인계서 — 주식 도구 모음 (KIS/DART 연동)

다음 세션을 맡는 Claude Code에게: 이 문서는 지금까지의 작업 맥락, 현재 상태,
알려진 이슈를 정리한 것이다. 작업을 이어받기 전에 전체를 읽어라.

## 프로젝트 개요

사용자는 소액(100~200만원 단위)으로 주식 투자를 직접 공부/운영하는 개인이고,
투자 판단에 참고할 **스크리닝/모니터링 도구**를 Python + Tkinter 데스크톱 GUI로
만들어달라고 요청해왔다. **이 저장소는 실제 주문을 넣는 자동매매 프로그램이
아니다** — 신호와 조건을 계산해서 보여줄 뿐, 매수/매도는 사용자가 증권사
앱/HTS에서 직접 판단해서 누른다. 이 경계는 사용자가 명시적으로 확인한
설계 원칙이니 앞으로도 유지해야 한다(자동 주문 기능을 임의로 추가하지 말 것 —
추가하려면 반드시 먼저 사용자에게 확인).

이 저장소엔 성격이 다른 두 프로그램이 있다:

1. **`gui.py`** — 대장주 자동 발굴기 (스윙 스크리닝, 분~시간 단위가 아니라
   일~주 단위 보유를 전제로 한 조건 필터)
2. **`scalper.py`** — 초단타 모니터링 (몇 분~몇 시간 단위 매매 참고용, 읽기 전용)

## 저장소/브랜치

- GitHub: `ug3303-prog/-` (저장소 이름이 `-` 한 글자다)
- 작업 브랜치: `claude/amazing-rubin-w62q9y` — 지금까지 모든 커밋이 여기 있다.
  이 리포에 PR을 만들라는 지시는 없었으므로 아직 PR 없음. 새 작업도 이 브랜치에
  계속 커밋하면 된다 (사용자가 다른 지시를 하지 않는 한).
- 사용자의 로컬 작업 폴더 경로(Windows, OneDrive 동기화 폴더 안):
  `C:\Users\User\OneDrive\바탕 화면\Personal\자산관리_주식_투자\AI_추천프로그램\대장주_스윙`
  (사용자가 "우량주_스윙"이라고 불렀다가 실제 폴더명은 "대장주_스윙"이었음 — 폴더명
  혼동이 있었으니 작업 전 `dir`로 실제 경로를 먼저 확인할 것)

## 지금까지 한 일 (커밋 순서대로)

1. `auto_finder.py`(원본 스크리닝 로직)를 받아서 `kis_feed.py`(KIS Open API
   래퍼)를 새로 작성 — 원본엔 이 모듈이 없었고 참조만 하고 있었음
2. `gui.py` Tkinter GUI 작성 — API 키 입력/저장, 조건 설정, 진행바, 결과표
3. 결과표 UX 개선 — 충족/칼날주의를 탭 대신 위아래로 항상 같이 표시
4. 거래대금(유동성) 필터 추가
5. PER/PBR 밸류에이션 필터 추가 (현재가 조회 API 응답에 이미 포함된 값 재사용)
6. **버그 수정 — 토큰 캐시 만료시각 누락**: 캐시된 토큰을 쓸 때 만료시각을
   같이 복원하지 않아서 매 요청마다 재발급을 시도 → KIS 레이트리밋(403) 유발.
   사용자가 겪은 실제 장애였고 지금은 고쳐짐
7. **버그 수정 — 시총 데이터 결측 처리**: `fdr.StockListing('KRX-MARCAP')`가
   GitHub 캐시 CSV를 쓰는데, 당일 데이터가 아직 안 채워지면 Marcap이 전부
   NaN으로 옴 → 정렬이 의미 없어지고 '대형주 상위 N개'가 사실상 무작위가 됨.
   지금은 캐시가 결측이면 하루씩 거슬러 올라가며 데이터 채워진 날짜를 찾도록
   고쳐짐(`load_top_marcap()`). KRX 서버 직접 조회는 시도했다가 방화벽/사내망
   이슈로 불안정해서 포기하고 GitHub 캐시 폴백으로 최종 결정함 — 이 경로를
   다시 KRX 직접조회로 되돌리지 말 것
8. DART(전자공시) 연동 — `dart_feed.py` 신규 작성: corp_code 매핑 캐싱,
   단일회사 주요계정 조회, 부채비율/영업이익·매출 YoY/영업흑자여부 계산.
   `run_scan()`에 `use_dart`/`max_debt_ratio`/`require_op_profit` 옵션 추가.
   **성능 고려**: 80종목 전체가 아니라 눌림 조건까지 통과한 후보에만 DART를
   조회하도록 구현되어 있음 — 이 순서를 바꾸면 스캔이 훨씬 느려진다
9. 코드 점검 커밋에서 같이 고친 것:
   - 모의투자(virtual) 레이트리밋 분리 (`_MIN_INTERVAL_REAL` vs
     `_MIN_INTERVAL_VIRTUAL`) — 모의투자가 실전보다 훨씬 엄격함
   - KIS가 HTTP 200 + 본문 `rt_cd`로 실패를 알리는 패턴을 무시하던 문제 수정
     (`_get()`에서 `rt_cd != "0"`이면 예외를 던지도록)
   - `run_scan()`이 모듈 전역 `LOOKBACK`을 직접 변경하던 구조 제거,
     `_fetch_history(..., lookback=...)` 파라미터로 전달하도록 변경
10. `scalper.py` 신규 작성 — 초단타 모니터링. `kis_feed.py`에
    `fetch_orderbook()`(호가 1~10단계, 매수/매도 총잔량비) 추가하고
    `fetch_realtime()`을 확장해 전일대비/등락률/누적거래량/체결강도(`cttr`)를
    포함시킴. 신호 로직(`Watcher` 클래스)은 가격 급등락, 체결강도 쏠림, 호가
    쏠림, 거래량 급증 네 가지를 계산한다. **주문 관련 코드는 전혀 없다** —
    `_poll_worker`는 `kf.fetch_realtime`/`kf.fetch_orderbook`만 호출한다.

11. **로컬 버전과 통합 (2026-11-04)** — 사용자 PC에서 pull이 계속 막혀 있었는데,
    확인해보니 로컬 코드는 채팅 복사본이 아니라 `926ec8f`에서 **별도로 크게 발전한
    버전**이었다(다른 도구로 받은 `auto_finder_fixed.zip` 등). 백업 브랜치
    `local-backup-1104`(`45b3aa1`)로 올려받아 비교 후, 사용자 선택에 따라
    **로컬 버전을 기준**으로 GitHub 쪽을 merge했다. 로컬 버전에 있던 것:
    추천 후보 3종목(점수화·업종 분산 — 매수 권유 아님), 외국인/기관 5일 수급
    (`fetch_investor_flow`), 일봉 디스크 캐시·종목 마스터 캐시, 장중 미완성 봉 처리,
    200일선 기울기, 금융업 부채비율 면제, 관리종목 제외, 조건값 저장(`gui_settings.json`),
    JWT 기반 토큰 만료, DART 캐시 버전·오류별 캐시 규칙, 루트 `test_auto_finder.py`.
    GitHub 쪽에서 옮겨 붙인 것: `scalper.py`, `fetch_orderbook()`, `fetch_realtime()`의
    전일대비/등락률/누적거래량/체결강도 필드(기존 키는 그대로, 키만 추가),
    HANDOFF.md, dart_key.example.json. 내가 만들었던 `tests/`·`_is_common_stock()`은
    로컬 구조(종목 마스터 레코드)와 안 맞아 버리고 아래 12번으로 대체.
12. **버그 수정 — 메리츠금융지주가 리츠로 분류돼 빠지던 문제**: `_df_to_records()`가
    이름에 '리츠'가 *포함*되면 RT로 분류해서, 2026-11-03 KRX 실데이터 기준
    메리츠금융지주(시총 21.8조)·블리츠웨이엔터테인먼트·메리츠제1/2호스팩이 잘못 분류됨.
    '리츠'로 *끝나는* 경우만 RT로 변경. (이 버그는 GitHub판 키워드 필터에도 있었다.)
    남은 예외: `이리츠코크렙`(이름이 리츠로 안 끝나는 리츠, 소형이라 기본값 영향 없음)
13. `.gitignore`에 로컬 버전이 만드는 캐시(`.krx_master_cache.json`, `.price_cache/`,
    `.last_scan.json`)와 `gui_settings.json`, `desktop.ini` 추가 — README엔 "git 제외"라고
    적혀 있었는데 실제로는 빠져 있었음

## 파일 구성

- `kis_feed.py` — KIS Open API 래퍼. 토큰 발급/캐시(JWT 만료), 일봉, 현재가+PER+PBR+
  업종+위험플래그+체결강도(`fetch_realtime`), 투자자 수급(`fetch_investor_flow`),
  호가(`fetch_orderbook`), 레이트리밋(`_throttle`, 모의/실전 분리)
- `dart_feed.py` — DART Open API 래퍼. corp_code 매핑 캐시(`.dart_corpcode_cache.json`,
  30일 TTL), 재무제표 캐시(`.dart_financials_cache.json`, 7일 TTL)
- `auto_finder.py` — 스윙 스크리닝·점수화 핵심 로직. `run_scan()`이 GUI 진입점(ScanResult 반환),
  `load_top_marcap()`이 종목 마스터(`_load_master_records`)에서 대형 보통주를 고름,
  `pick_top()`이 추천 후보 3종목 선정
- `test_auto_finder.py` — 단위 테스트(`python -m unittest test_auto_finder`, 네트워크·키 불필요)
- `gui.py` — 대장주 자동 발굴기 GUI (스윙)
- `scalper.py` — 초단타 모니터링 GUI (신호 전용)
- `diag_marcap.py` — KRX 시총 데이터 진단용 일회성 스크립트 (디버깅 때 만듦,
  정식 기능 아님 — 필요 없어지면 삭제해도 됨)
- `requirements.txt` — `finance-datareader`, `requests>=2.28`, `pandas>=1.5` (tkinter는 표준 라이브러리)
- `kis_keys.example.json`, `dart_key.example.json` — 키 포맷 예시. 실제 키 파일
  (`kis_keys.json`, `dart_key.json`, `.kis_token_cache.json`,
  `.dart_corpcode_cache.json`, `.dart_financials_cache.json`)은 `.gitignore`에
  있어서 커밋되지 않음

## 알려진 이슈 / 주의할 점

1. **보안**: 사용자의 작업 폴더가 OneDrive 동기화 폴더 안에 있다. `kis_keys.json`
   등은 평문 저장이라 OneDrive로 그대로 업로드된다. README에 경고는 적어뒀지만
   폴더를 옮기라고 강제하진 않았다 — 사용자가 선택할 문제.
2. **사용자 PC 동기화 — 2026-11-04 완료**: PC가 `claude/amazing-rubin-w62q9y`의
   통합 커밋(`d7a3847`)과 일치함을 `git status`로 확인. 남은 로컬 상태:
   `kis_keys.example.json` 수정분(키가 있을 수 있어 일부러 커밋 안 함),
   untracked `auto_finder_fixed.zip`·`gitignore`(점 없는 옛 파일)·투자 판단 기준 메모 txt
   (사용자 개인 메모 — 건드리지 말 것), 예전 `stash@{0}`(백업 브랜치와 같은 내용).
   백업: 원격 브랜치 `local-backup-1104`, PC 폴더 `대장주_스윙_백업_1104`.
   정리(삭제)는 사용자 판단에 맡김 — 임의로 지우라고 하지 말 것.
   **이 저장소는 public이다** — 키가 들어갈 수 있는 파일은 절대 커밋하지 말 것.
3. **체결강도 — 2026-11-04 실사용 버그 수정**: 처음엔 현재가 응답의
   `shnu_cntg_qty`/`seln_cntg_qty`로 계산했는데, 실사용에서 체결강도가 항상 `-`로 나옴.
   KIS 공식 샘플(github.com/koreainvestment/open-trading-api)을 확인해보니 그런 필드는
   없었다. **주식현재가 체결 API(inquire-ccnl, FHKST01010300)의 `tday_rltv`(당일 체결강도)**로
   교체(`kis_feed.fetch_cttr()`). 필드명은 공식 샘플로 확인했지만 실제 응답으로 값이
   나오는지는 사용자 재확인 필요. KIS 필드를 새로 쓸 땐 추측하지 말고 이 공식 샘플
   저장소(`examples_llm/domestic_stock/*/chk_*.py`의 COLUMN_MAPPING)부터 볼 것.
4. **호가 API(`fetch_orderbook`)는 실사용 확인됨** — 2026-11-04 코웨이로 호가매수/매도비
   0.85 정상 표시. 필드명도 공식 샘플과 일치.
5. **`scalper.py`는 종목당 폴링 1회에 API 호출 3번**(현재가+호가+체결강도)을 쓴다.
   워치리스트가 크면 폴링 주기 안에 못 끝나거나 레이트리밋에 걸릴 수 있음 —
   README에 5~10종목 권장이라고 적어뒀다.
6. **DART 계정명 매칭이 완벽하지 않을 수 있음** — `_ACCOUNT_ALIASES`에 흔한
   계정명(`영업이익`, `영업이익(손실)` 등)만 등록해뒀다. 일부 회사가 다른
   표현을 쓰면 그 종목은 재무 데이터 없이(None) 통과된다 — 의도된 동작(필터링
   대상에서 빼는 게 아니라 "모르니 통과시킨다"는 원칙 유지 중).

## 테스트 방법 (이 환경은 tkinter/실제 API 접근이 없음)

- 문법 검증: `python3 -c "import ast; ast.parse(open('파일명').read())"`
- 단위 테스트: 저장소 루트에서 `python -m unittest test_auto_finder` (KIS/DART는 `unittest.mock.patch`로 대체)
- 시총 관련 수정은 FinanceData 캐시 CSV(`raw.githubusercontent.com/FinanceData/fdr_krx_data_cache/...`)로
  실데이터 검증 가능 — 이 환경에서 접근됨
- tkinter가 없는 환경에서 GUI 모듈 로직만 테스트하려면 `sys.modules`에
  `tkinter`/`tkinter.ttk`/`tkinter.messagebox` 더미 모듈을 올리고 import하면
  (`Watcher` 같은 순수 로직 클래스는) 테스트 가능 — `scalper.py`의 `Watcher`
  클래스가 그 예시

## 다음에 할 만한 작업 후보 (요청받은 적은 없음, 참고용)

- `scalper.py`의 체결강도(`tday_rltv`) 값이 실제로 나오는지 사용자 재확인
- `gui.py`/`scalper.py`의 조건값들을 설정 파일로 분리해 재사용성 높이기
- DART 재무제표 API 호출 실패 시 사유를 로그에 더 자세히 남기기

## 톤/운영 원칙 (사용자 선호)

- 사용자는 반말(존대말 없이) 응답을 선호한다고 명시함
- 사용자는 "공부 삼아 운영"한다고 했음 — 과도한 자동화나 리스크 확대 제안은
  하지 말고, 매번 "참고용이지 매수 신호 아님"을 분명히 할 것
- 코드 수정 후엔 항상 (가능한 범위에서) 로직 테스트 후 커밋 메시지에 근거를
  명확히 적고 push까지 완료하는 패턴을 유지해왔음 — 이 흐름을 이어갈 것
