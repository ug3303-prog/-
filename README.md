# 대장주 자동 발굴기 (GUI)

`auto_finder.py`의 스크리닝 로직(대형주 + 상승추세 + 눌림 + 거래대금 + PER/PBR + DART 재무)을
Tkinter 데스크톱 GUI로 감싼 프로그램.

## 설치

```bash
pip install -r requirements.txt
```

## 실행

```bash
python gui.py
```

1. 상단에 KIS(한국투자증권) Open API `APP KEY` / `APP SECRET` 입력 (모의투자면 체크박스 선택)
2. "키 저장"을 누르면 `kis_keys.json`에 저장되어 다음 실행부터 자동 로딩됨 (git에는 커밋되지 않음)
3. DART 재무정보(부채비율, 영업이익 흑자여부 등)를 쓰려면 `DART API KEY`도 입력하고
   "DART 재무정보 사용" 체크 — 종목마다 추가 조회가 붙어 스캔이 느려짐
4. 조건(시총 상위 N개, 최소 시총, 눌림 기준 등) 확인 후 "▶ 스캔 시작"
5. 결과는 "조건 충족" / "칼날 주의(하락추세)" 표에 표시, CSV로도 저장 가능

## 파일 구성

- `kis_feed.py` — KIS Open API 최소 래퍼 (토큰 발급/캐시, 일봉/현재가/PER/PBR 조회, 레이트리밋)
- `dart_feed.py` — DART(전자공시) Open API 최소 래퍼 (부채비율, 영업이익/매출 YoY 등)
- `auto_finder.py` — 스크리닝 로직 (`run_scan()` 이 GUI에서 호출하는 핵심 함수), CLI로도 실행 가능(`python auto_finder.py`)
- `gui.py` — Tkinter GUI (실제 실행 파일)
- `diag_marcap.py` — KRX 시총 데이터 문제 진단용 스크립트

## API 키 준비

- **KIS**: [한국투자증권 Open API](https://apiportal.koreainvestment.com)에서 APP KEY/SECRET 발급 (모의/실전 각각 별도)
- **DART**: [opendart.fss.or.kr](https://opendart.fss.or.kr)에서 무료로 인증키 발급 (필수 아님 — 없어도 KIS 조건만으로 동작)

## 보안 주의 — 꼭 읽어주세요

`kis_keys.json`, `dart_key.json`, `.kis_token_cache.json` 은 **API 키/토큰이 평문으로 저장**됩니다.
`.gitignore`로 git에는 안 올라가지만, 이 폴더가 **OneDrive/Dropbox 같은 클라우드 동기화 폴더 안에
있으면 그쪽으로는 그대로 업로드**됩니다. 실전투자 키를 쓴다면:
- 이 폴더를 동기화 제외 목록에 추가하거나
- 동기화 안 되는 로컬 폴더(예: `C:\dev\...`)로 프로그램을 옮겨서 실행하는 걸 권장합니다.

## 주의

- 추천이 아니라 조건 스크리닝 도구입니다. 매수 여부·종목·금액은 본인 판단.
- 대형주만 대상이며, 종목 수가 많을수록(TOP_N↑) 스캔 시간이 길어집니다(종목당 API 호출 여러 번).
- DART 재무정보는 공시 주기(분기)상 최신이 아닐 수 있고, 일부 종목은 계정명이 달라 조회가 안 될 수 있습니다(이 경우 필터 대상에서 제외하지 않고 통과시킵니다).
