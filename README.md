# 대장주 자동 발굴기 (GUI)

`auto_finder.py`의 스크리닝 로직(대형주 + 상승추세 + 눌림)을 Tkinter 데스크톱 GUI로 감싼 프로그램.

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
3. 조건(시총 상위 N개, 최소 시총, 눌림 기준 등) 확인 후 "▶ 스캔 시작"
4. 결과는 "조건 충족" / "칼날 주의(하락추세)" 탭에 표로 표시, CSV로도 저장 가능

## 파일 구성

- `kis_feed.py` — KIS Open API 최소 래퍼 (토큰 발급/캐시, 일봉/현재가 조회, 레이트리밋)
- `auto_finder.py` — 스크리닝 로직 (`run_scan()` 이 GUI에서 호출하는 핵심 함수), CLI로도 실행 가능(`python auto_finder.py`)
- `gui.py` — Tkinter GUI (실제 실행 파일)

## 주의

- 추천이 아니라 조건 스크리닝 도구입니다. 매수 여부·종목·금액은 본인 판단.
- 대형주만 대상이며, 종목 수가 많을수록(TOP_N↑) 스캔 시간이 길어집니다(종목당 API 호출 여러 번).
