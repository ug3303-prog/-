# -*- coding: utf-8 -*-
"""
단타 스캐너 v16 — 독립 실행 GUI
════════════════════════════════
danta_v16.py(채점 엔진)를 창 프로그램으로 감싼 버전.
  · KIS 키 입력/저장 · 조건 입력 · 스캔 시작/중단 · 진행률 · 결과 표 · 로그
  · 결과 CSV 저장 · 엑셀 [🎯단타추천]/[⛔차단종목]/[포지션추천]/[매매일지] 기록(선택)
  · 자동 반복(N분) · 보유 종목 점검 · 추천 자동 채점 · 연결 점검

같은 폴더에 필요: danta_v16.py  (종목풀 엑셀 불필요 — 스캔 대상은 KIS에서 자동 수집)
실행: 단타스캐너_실행.bat 더블클릭 (또는 pythonw 단타스캐너_GUI.py)
"""
import contextlib
import csv
import io
import json
import queue
import sys
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import openpyxl

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
WORK_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
sys.path.insert(0, str(APP_DIR))

import danta_v16 as D  # noqa: E402  (채점 엔진)

# exe로 묶었을 때도 설정·캐시·엑셀은 exe 옆 폴더를 쓰도록
D.SCRIPT_DIR = WORK_DIR
D.CONFIG_PATH = WORK_DIR / "kis_config.json"
D.TOKEN_PATH = WORK_DIR / ".kis_token.json"
D.CACHE_DIR = WORK_DIR / "cache_v16"

SETTINGS_PATH = WORK_DIR / "gui_settings.json"
RECORD_XLSX = WORK_DIR / "단타스캐너_기록.xlsx"
FONT = ("맑은 고딕", 9)
FONT_B = ("맑은 고딕", 9, "bold")

DEFAULTS = {
    "capital": "9000000", "positions": "3", "stop": "5", "risk": "1",
    "mcap_min": "2000", "mcap_max": "10000", "min_amount": "20",
    "th_lead": "60", "th_wait": "45", "th_watch": "28",
    "repeat_min": "0", "write_excel": True, "max_n": "150", "use_rank": True,
}

CAND_COLS = [("grade", "판정", 90), ("name", "종목명", 120), ("code", "코드", 65), ("sector", "섹터", 85),
             ("score", "점수", 50), ("price", "현재가", 80), ("chg", "등락%", 60), ("vol", "거래량(배)", 70),
             ("vwap", "VWAP", 75), ("strength", "체결강도", 65), ("mcap", "시총(억)", 75),
             ("atr", "ATR%", 55), ("stop", "손절가", 75), ("target", "목표가", 75), ("action", "행동", 85),
             ("note", "선도/진입 제외 사유", 190), ("signal", "매수 근거", 330)]
BLOCK_COLS = [("name", "종목명", 120), ("code", "코드", 65), ("sector", "섹터", 85), ("price", "현재가", 80),
              ("chg", "등락%", 60), ("reason", "차단 사유", 700)]
PORT_COLS = [("name", "종목명", 120), ("sector", "섹터", 85), ("grade", "등급", 90), ("score", "점수", 50),
             ("price", "현재가", 80), ("shares", "수량", 60), ("invested", "투입금액", 95),
             ("stop_price", "손절가(자동주문)", 110), ("target_price", "목표가(자동주문)", 110),
             ("stop_pct", "손절%", 55), ("target_pct", "목표%", 55), ("atr_pct", "ATR%", 55), ("basis", "손절기준", 70),
             ("max_loss", "최대손실", 85), ("max_gain", "최대이익", 85)]

GRADE_TAG = {"🚀선도매수": "lead", "⬆매수대기": "wait", "👀관심": "watch", "⬇제외": "out", "⛔차단": "block"}


class QueueWriter(io.TextIOBase):
    """엔진의 print() 출력을 로그 창으로 보냄."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(("log", s))
        return len(s)


def fmt_int(v):
    try:
        return f"{int(round(float(v))):,}"
    except Exception:
        return "-" if v in (None, "") else str(v)


def fmt_f(v, n=1):
    try:
        return f"{float(v):.{n}f}"
    except Exception:
        return "-"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("단타 스캐너 v17 — KIS 실시간")
        self.geometry("1500x900")
        self.minsize(1100, 700)
        self.option_add("*Font", FONT)
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=22, font=FONT)
        style.configure("Treeview.Heading", font=FONT_B)
        style.configure("green.Horizontal.TProgressbar", background="#1f9d3a")

        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.results, self.portfolio, self.regime = [], [], {}
        self.repeat_job = None
        self.settings = self._load_settings()

        self._build_keys()
        self._build_conditions()
        self._build_controls()
        self._build_tables()
        self._build_log()
        self._load_keys_into_form()
        self.after(100, self._poll)
        self.after(1000, self._tick_clock)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log("단타 스캐너 v17 준비 완료. KIS 키를 넣고 [키 저장] → [연결 점검] → [▶ 스캔 시작] 순서로 쓰면 돼.\n"
                 "스캔 대상은 KIS 종목 마스터(코스피·코스닥 전 종목)에서 조건으로 자동 수집해.\n")

    # ─────────────────────────── UI 구성 ───────────────────────────
    def _build_keys(self):
        f = ttk.LabelFrame(self, text="KIS API 키")
        f.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(f, text="APP KEY").grid(row=0, column=0, padx=(8, 4), pady=6, sticky="w")
        self.v_key = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_key, show="•", width=42).grid(row=0, column=1, padx=4)
        ttk.Label(f, text="APP SECRET").grid(row=0, column=2, padx=(14, 4))
        self.v_secret = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_secret, show="•", width=48).grid(row=0, column=3, padx=4)
        ttk.Label(f, text="DART 키(선택)").grid(row=0, column=4, padx=(14, 4))
        self.v_dart = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_dart, show="•", width=28).grid(row=0, column=5, padx=4)
        self.v_mock = tk.BooleanVar()
        ttk.Checkbutton(f, text="모의투자 서버", variable=self.v_mock).grid(row=0, column=6, padx=10)
        ttk.Button(f, text="키 저장", command=self.save_keys).grid(row=0, column=7, padx=4)
        ttk.Button(f, text="연결 점검", command=self.check_connection).grid(row=0, column=8, padx=(4, 8))

    def _build_conditions(self):
        f = ttk.LabelFrame(self, text="발굴 조건")
        f.pack(fill="x", padx=8, pady=4)
        self.vars = {}

        def field(r, c, key, label, w=10):
            ttk.Label(f, text=label).grid(row=r, column=c, padx=(8, 4), pady=4, sticky="e")
            v = tk.StringVar(value=str(self.settings.get(key, DEFAULTS[key])))
            ttk.Entry(f, textvariable=v, width=w).grid(row=r, column=c + 1, padx=(0, 6), sticky="w")
            self.vars[key] = v

        field(0, 0, "capital", "총 자본(원)", 12)
        field(0, 2, "positions", "보유 종목 수", 5)
        field(0, 4, "stop", "손절 상한(%)", 5)
        field(0, 6, "risk", "종목당 최대손실(자본%)", 5)
        field(0, 8, "mcap_min", "시총 최소(억)", 8)
        field(0, 10, "mcap_max", "시총 최대(억)", 8)
        field(0, 12, "min_amount", "최소 평균거래대금(억)", 6)
        field(1, 0, "th_lead", "🚀선도매수 ≥", 5)
        field(1, 2, "th_wait", "⬆매수대기 ≥", 5)
        field(1, 4, "th_watch", "👀관심 ≥", 5)
        field(1, 6, "repeat_min", "자동 반복(분, 0=끔)", 5)

        self.v_excel = tk.BooleanVar(value=bool(self.settings.get("write_excel", True)))
        ttk.Checkbutton(f, text="엑셀 기록 (단타스캐너_기록.xlsx)", variable=self.v_excel).grid(
            row=1, column=8, columnspan=2, padx=8, sticky="w")
        field(1, 10, "max_n", "스캔 대상 최대", 6)
        self.v_rank = tk.BooleanVar(value=bool(self.settings.get("use_rank", True)))
        ttk.Checkbutton(f, text="오늘 거래대금·증가율 상위 우선", variable=self.v_rank).grid(
            row=1, column=12, columnspan=2, padx=8, sticky="w")

    def _build_controls(self):
        f = ttk.Frame(self)
        f.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(f, text="▶ 스캔 시작", command=self.start_scan, width=12)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(f, text="■ 중단", command=self.stop_scan, state="disabled", width=8)
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(f, text="결과 CSV 저장", command=self.save_csv).pack(side="left", padx=4)
        ttk.Button(f, text="추천 자동 채점", command=self.review).pack(side="left", padx=4)
        ttk.Separator(f, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Label(f, text="보유 점검").pack(side="left")
        self.v_hold = tk.StringVar(value=self.settings.get("holding", ""))
        ttk.Entry(f, textvariable=self.v_hold, width=28).pack(side="left", padx=4)
        ttk.Button(f, text="점검", command=self.check_holding, width=5).pack(side="left")
        self.progress = ttk.Progressbar(f, mode="determinate", style="green.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.v_status = tk.StringVar(value="대기")
        ttk.Label(f, textvariable=self.v_status, width=38).pack(side="left")

        g = ttk.Frame(self)
        g.pack(fill="x", padx=8)
        self.v_clock = tk.StringVar()
        self.v_regime = tk.StringVar(value="시장 레짐: -")
        ttk.Label(g, textvariable=self.v_clock, font=FONT_B).pack(side="left")
        ttk.Label(g, textvariable=self.v_regime, font=FONT_B, foreground="#1B3A5C").pack(side="left", padx=20)

    def _make_tree(self, parent, cols):
        frame = ttk.Frame(parent)
        tv = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, label, w in cols:
            tv.heading(key, text=label, command=lambda k=key, t=tv: self._sort(t, k, False))
            anchor = "w" if key in ("name", "signal", "reason", "sector") else "center"
            tv.column(key, width=w, anchor=anchor, stretch=key in ("signal", "reason"))
        ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        tv.grid(row=0, column=0, sticky="nsew"); ys.grid(row=0, column=1, sticky="ns"); xs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        for tag, bg in (("lead", "#C6EFCE"), ("wait", "#E2EFDA"), ("watch", "#FFF9E0"),
                        ("out", "#F2F2F2"), ("block", "#FFD9DE")):
            tv.tag_configure(tag, background=bg)
        return frame, tv

    def _build_tables(self):
        pw = ttk.PanedWindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=8, pady=4)
        self.nb = ttk.Notebook(pw)
        f1, self.tv_cand = self._make_tree(self.nb, CAND_COLS)
        f2, self.tv_port = self._make_tree(self.nb, PORT_COLS)
        f3, self.tv_block = self._make_tree(self.nb, BLOCK_COLS)
        self.nb.add(f1, text="🎯 매수 후보")
        self.nb.add(f2, text="💰 오늘의 추천 포트폴리오")
        self.nb.add(f3, text="⛔ 차단 종목 (매수 금지)")
        pw.add(self.nb, weight=5)

        det = ttk.LabelFrame(pw, text="선택 종목 상세 (행 클릭)")
        self.txt_detail = tk.Text(det, height=4, wrap="word", font=FONT, relief="flat", background="#FAFAFA")
        self.txt_detail.pack(fill="both", expand=True, padx=4, pady=4)
        pw.add(det, weight=1)
        self._pw = pw
        for tv in (self.tv_cand, self.tv_block, self.tv_port):
            tv.bind("<<TreeviewSelect>>", self._on_select)

    def _build_log(self):
        f = ttk.LabelFrame(self._pw, text="로그")
        self.txt_log = tk.Text(f, height=8, wrap="word", font=("Consolas", 9), background="#FFFFFF")
        ys = ttk.Scrollbar(f, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=ys.set)
        self.txt_log.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        ys.pack(side="left", fill="y", pady=4)
        self._pw.add(f, weight=2)

    # ─────────────────────────── 설정/키 ───────────────────────────
    def _load_settings(self):
        try:
            return {**DEFAULTS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
        except Exception:
            return dict(DEFAULTS)

    def _save_settings(self):
        s = {k: v.get() for k, v in self.vars.items()}
        s.update(write_excel=self.v_excel.get(), use_rank=self.v_rank.get(), holding=self.v_hold.get())
        try:
            SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_keys_into_form(self):
        try:
            cfg = json.loads(D.CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if "여기에" not in str(cfg.get("app_key", "")):
            self.v_key.set(cfg.get("app_key", "")); self.v_secret.set(cfg.get("app_secret", ""))
        self.v_dart.set(cfg.get("dart_api_key", ""))
        self.v_mock.set(str(cfg.get("mode", "real")).lower().startswith("mock"))

    def save_keys(self):
        if not self.v_key.get().strip() or not self.v_secret.get().strip():
            messagebox.showwarning("키 저장", "APP KEY와 APP SECRET을 모두 넣어줘."); return
        cfg = {"app_key": self.v_key.get().strip(), "app_secret": self.v_secret.get().strip(),
               "mode": "mock" if self.v_mock.get() else "real", "dart_api_key": self.v_dart.get().strip()}
        D.CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        D._KIS = None; D._DART = None          # 새 키로 다시 연결
        self.log(f"키 저장 완료 → {D.CONFIG_PATH.name} ({'모의' if self.v_mock.get() else '실전'} 서버)\n")

    # ─────────────────────────── 파라미터 적용 ───────────────────────────
    def _params(self):
        try:
            p = {k: float(v.get().replace(",", "")) for k, v in self.vars.items()}
        except ValueError:
            messagebox.showerror("입력 오류", "발굴 조건에는 숫자만 넣어줘."); return None
        if not (p["th_lead"] > p["th_wait"] > p["th_watch"]):
            messagebox.showerror("입력 오류", "등급 기준은 선도매수 > 매수대기 > 관심 순이어야 해."); return None
        p["use_rank"] = self.v_rank.get()
        p["profit"] = p["stop"] * D.REWARD_RISK          # ATR 계산이 안 될 때만 쓰는 예비 목표
        D.RISK_PER_TRADE_PCT = p["risk"]
        p["book"] = RECORD_XLSX
        # 엔진 전역값에 반영
        D.GRADE = {"선도매수": p["th_lead"], "매수대기": p["th_wait"], "관심": p["th_watch"]}
        D.MCAP_FILTER_ON = not (p["mcap_min"] <= 0 and p["mcap_max"] <= 0)
        D.MCAP_MIN = p["mcap_min"]
        D.MCAP_MAX = p["mcap_max"] if p["mcap_max"] > 0 else 10 ** 12
        D.MIN_AVG_AMOUNT_EOK = p["min_amount"]
        D.STOP_CAP_PCT = p["stop"]                       # 하루 변동폭이 손절 상한보다 큰 종목 걸러내기
        return p

    # ─────────────────────────── 스캔 ───────────────────────────
    def start_scan(self, auto=False):
        if self.worker and self.worker.is_alive():
            return
        p = self._params()
        if not p:
            return
        self._save_settings()
        self.stop_event.clear()
        self.btn_start.configure(state="disabled"); self.btn_stop.configure(state="normal")
        self.progress["value"] = 0
        self.v_status.set("스캔 준비 중...")
        self.worker = threading.Thread(target=self._scan_worker, args=(p,), daemon=True)
        self.worker.start()

    def stop_scan(self):
        self.stop_event.set()
        if self.repeat_job:
            self.after_cancel(self.repeat_job); self.repeat_job = None
        self.v_status.set("중단 요청됨 — 현재 종목까지 처리 후 멈춤")

    def _scan_worker(self, p):
        q = self.q
        try:
            with contextlib.redirect_stdout(QueueWriter(q)):
                D.init_feeds()
            q.put(("log", f"\n[{datetime.now():%H:%M:%S}] 스캔 대상 수집 중 (KIS 종목 마스터 + 거래 순위)...\n"))
            q.put(("progress", (0, "스캔 대상 수집 중...")))
            pool = D.build_universe(p["mcap_min"], p["mcap_max"], p["min_amount"], int(p["max_n"]),
                                    p["use_rank"], log=lambda m: q.put(("log", m)))
            q.put(("log", f"[{datetime.now():%H:%M:%S}] 스캔 시작 — 대상 {len(pool)}개\n"))
            q.put(("max", len(pool)))
            mst = D.market_status()
            cache = D.DayCache()
            today = datetime.now().strftime("%Y%m%d")
            calls0 = D._KIS.calls
            collected, fails = [], 0
            for i, st in enumerate(pool, 1):
                if self.stop_event.is_set():
                    q.put(("log", "중단됨.\n")); break
                code = st["code"]
                try:
                    hist = cache.get_or(f"d:{code}", lambda: [d for d in (D.fetch_daily(code) or []) if d["date"] < today] or None)
                    rt = D.fetch_realtime(code)
                    inv = cache.get_or(f"i:{code}", lambda: D._safe(D.fetch_investor, code, None)) or {}
                    fil = cache.get_or(f"f:{code}", lambda: D._safe(D.fetch_filings, code, None)) or []
                    if hist and rt:
                        st["sector"] = rt.get("sector") or st.get("sector") or "기타"
                        collected.append((st, hist, rt, inv, fil))
                    else:
                        fails += 1; q.put(("log", f"  ✗ {st['name']}({code}) 시세 없음\n"))
                except D.KISError as e:
                    fails += 1; q.put(("log", f"  ✗ {st['name']}({code}) {e}\n"))
                q.put(("progress", (i, f"조회 {i}/{len(pool)} — {st['name']}")))
            cache.save()
            if not collected:
                q.put(("done", None)); return

            sec = {}
            for st, _, rt, _, _ in collected:
                sec.setdefault(st["sector"], []).append(rt.get("change_rate") or 0)
            sec_avg = {k: sum(v) / len(v) for k, v in sec.items() if len(v) >= 2}

            pre = [D.calc_score_v15(h, rt, inv, False, None, fil, sec_avg.get(st["sector"]))
                   for st, h, rt, inv, fil in collected]
            regime = D.assess_regime_market([(None, sc) for sc in pre], mst)
            mgate = D.assess_market_gate()
            th = D.combine_thresholds(regime, mgate)
            regime = {**regime, "thresholds": th, "gate": mgate}
            lab = regime.get("label", "")
            momentum = ("우호" in lab and "비우호" not in lab
                        and not mgate.get("extra") and not mgate.get("block"))     # 지수까지 양호할 때만
            t_label, t_desc = D.current_time_strategy()

            q.put(("progress", (len(pool), "채점 중 (후보 체결강도 조회)...")))
            results = []
            for (st, h, rt, inv, fil), sc0 in zip(collected, pre):
                strength = None
                if mst == "OPEN" and not sc0["blocks"] and sc0["total"] >= th["관심"] - 10 and not self.stop_event.is_set():
                    strength = D._safe(D.fetch_strength, st["code"], None)
                sc = D.finalize_grade(D.calc_score_v15(h, rt, inv, momentum, strength, fil, sec_avg.get(st["sector"])), th)
                price = rt["price"]
                plan = D.trade_plan(price, h, p["stop"], rt.get("today_low"))
                results.append({
                    "name": st["name"], "code": st["code"], "sector": st["sector"], "grade": sc["grade"],
                    "score": sc["total"], "price": price, "change_rate": sc.get("change_rate"),
                    "vol_accel": sc.get("vol_accel"), "vwap": sc.get("vwap"), "strength": strength,
                    "mcap": sc.get("mcap"), "stop": plan["stop"], "target": plan["target"], "plan": plan,
                    "atr_pct": plan.get("atr_pct"), "signals": sc["signals"],
                    "warnings": sc["warnings"], "blocks": sc["blocks"], "bd": sc.get("bd", {}),
                    "gap_rate": sc.get("gap_rate"), "range_pos": sc.get("range_pos"),
                    "prog_net": sc.get("prog_net"), "fore_net": sc.get("fore_net"), "inst_net": sc.get("inst_net"),
                    "ma20": sc.get("ma20"), "ma60": sc.get("ma60"), "pullback": sc.get("pullback"),
                    "supply_score": sc.get("bd", {}).get("⑤수급"),
                    "action": "", "lead_block": sc.get("lead_block", []),
                    "rets20": sc.get("rets20"), "disparity": sc.get("disparity"),
                })
            n_lead = D.cap_lead_count(results, int(p["positions"]) * 2)
            for r in results:
                r["action"] = self._action(r["grade"], t_label, r.get("lead_block"), mgate, mst)
            q.put(("log", f"  선도매수 {n_lead}개 (정원: 보유 종목 수 × 2 = {int(p['positions']) * 2}개)\n"))
            order = {"🚀선도매수": 0, "⬆매수대기": 1, "👀관심": 2, "⬇제외": 3, "⛔차단": 4}
            results.sort(key=lambda r: (order.get(r["grade"], 9), -r["score"]))
            portfolio = self._portfolio(results, p)
            for r in results:                                   # 분산 규칙으로 빠진 종목은 사유 표시
                if r.get("skip_reason") and r["action"] == "진입 가능":
                    r["action"] = "보류 (" + r["skip_reason"] + ")"
            if mgate.get("block"):
                q.put(("log", f"  ⛔ 신규 진입 중단 — {mgate.get('block_reason')} (오늘은 포트폴리오 없음)\n"))
            elif not portfolio:
                q.put(("log", "  진입 가능한 종목 없음 — 현금 보유 (억지로 채우지 않음)\n"))

            q.put(("log", f"[{datetime.now():%H:%M:%S}] 완료 — 조회 {len(collected)} / 실패 {fails} / "
                          f"KIS 호출 {D._KIS.calls - calls0}회\n"))
            q.put(("log", f"  시장 레짐: {lab} — {regime.get('desc', '')}\n"))
            q.put(("log", f"  지수 게이트: {mgate['desc']}\n"))
            q.put(("log", f"  최종 등급 기준: 선도≥{th['선도매수']:.0f} 대기≥{th['매수대기']:.0f} 관심≥{th['관심']:.0f}\n"))
            q.put(("log", f"  시간대: {t_label} — {t_desc}\n"))
            if momentum:
                q.put(("log", "  🚀 모멘텀 모드 ON (상승장: 상단 돌파·주도주 우선)\n"))
            q.put(("result", (results, portfolio, regime, t_label)))

            if p and self.v_excel.get():
                self._write_excel(p["book"], results, portfolio, regime, p)
        except D.KISError as e:
            q.put(("error", f"KIS 연결 실패: {e}\n\n키와 실전/모의 설정을 확인해줘."))
        except Exception:
            q.put(("error", traceback.format_exc()))
        finally:
            q.put(("done", None))

    @staticmethod
    def _action(grade, t_label, issues=None, mgate=None, mst="OPEN"):
        """선도매수 또는 '품질 조건을 모두 통과한' 매수대기만 진입 가능. 나머지는 관찰."""
        if grade == "⛔차단":
            return "매수 금지"
        real = [x for x in (issues or []) if not x.startswith("선도 정원")]
        if grade == "👀관심" and real:
            return "관찰 (조건 미달)"
        if grade not in ("🚀선도매수", "⬆매수대기"):
            return ""
        if real:
            return "관찰 (조건 미달)"
        if (mgate or {}).get("block"):
            return "관찰 (지수 하락)"
        if mst != "OPEN" or not D.entry_window_ok(t_label):
            return "관찰 (진입시간 아님)"
        if "선도진입" in t_label:
            return "진입 가능" if grade == "🚀선도매수" else "대기"
        if grade == "⬆매수대기" and issues:
            return "대기 (정원 초과)"
        return "진입 가능"

    @staticmethod
    def _portfolio(results, p):
        """섹터당 최대 2종목 · ATR 손절/목표(호가단위) · 종목당 손실 ≤ 자본 risk%."""
        # v17: 화면에 '진입 가능'으로 뜬 종목만 (진입시간·지수 게이트·품질 조건 통과) —
        #      매매일지 복기에서 11:30 이후(소강구간) 추천 8건이 전부 손실이었다.
        prim = sorted([r for r in results if r["grade"] == "🚀선도매수" and r.get("action") == "진입 가능"],
                      key=lambda r: -r["score"]) + \
            sorted([r for r in results if r["grade"] == "⬆매수대기" and r.get("action") == "진입 가능"],
                   key=lambda r: -r["score"])
        picks = D.pick_diversified(prim, int(p["positions"]))      # 섹터당 1개 + 같이 움직이는 종목 중복 금지
        rows = []
        for r in picks:
            row = D.size_position(r, p["capital"], int(p["positions"]), p["stop"], p["profit"], p["risk"])
            if row:
                rows.append(row)
        return rows

    def _write_excel(self, xlsx, results, portfolio, regime, p):
        try:
            if Path(xlsx).exists():
                wb = openpyxl.load_workbook(xlsx)
            else:
                wb = openpyxl.Workbook(); wb.active.title = "안내"
                wb.active["A1"] = "단타 스캐너 v16 기록 파일 — 스캔할 때마다 자동 갱신 (매매일지는 누적)"
            now_str = datetime.now().strftime("%H:%M")
            D.write_scanner_sheet(wb, results, regime, now_str)
            D.write_blocked_sheet(wb, results, now_str)
            n = 0
            if portfolio:
                D.write_recommendation_sheet(wb, portfolio, p["capital"], p["stop"], p["profit"], now_str)
                n = D.append_trade_log(wb, portfolio, datetime.now())
            wb.save(xlsx)
            self.q.put(("log", f"  엑셀 기록 완료 ({Path(xlsx).name}){f' — 매매일지 신규 {n}건' if n else ''}\n"))
        except PermissionError:
            self.q.put(("log", "  ✗ 엑셀이 열려 있어 기록 못 함 — 엑셀을 닫고 다시 스캔해줘.\n"))
        except Exception as e:
            self.q.put(("log", f"  ✗ 엑셀 기록 실패: {e}\n"))

    # ─────────────────────────── 부가 기능 (엔진 함수 재사용) ───────────────────────────
    def _run_engine(self, title, fn):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(title, "스캔이 끝난 뒤에 실행해줘."); return
        self._params()

        def job():
            try:
                with contextlib.redirect_stdout(QueueWriter(self.q)):
                    fn()
            except D.KISError as e:
                self.q.put(("error", f"{title} 실패: {e}"))
            except Exception:
                self.q.put(("error", traceback.format_exc()))
            finally:
                self.q.put(("idle", title))
        self.v_status.set(f"{title} 실행 중...")
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def check_connection(self):
        self._params()
        self.log(f"\n[{datetime.now():%H:%M:%S}] 연결 점검\n")

        def job():
            D.init_feeds()
            rt = D.fetch_realtime("005930")
            print(f"  ✅ 현재가 조회: 삼성전자 {rt['price']:,}원 ({rt['change_rate']:+.2f}%) 업종 {rt.get('sector')}")
            d = D.fetch_daily("005930")
            print(f"  ✅ 일봉 {len(d)}개 (최근 {d[0]['date']})")
            print(f"  {'✅' if D._safe(D.fetch_investor, '005930', {}) else '❌'} 투자자 동향   "
                  f"체결강도 {D._safe(D.fetch_strength, '005930', None)}")
            try:
                r = D.fetch_rank("1001", "3")
                print(f"  ✅ 코스닥 거래대금 순위 {len(r)}개 (1위 {r[0]['name'] if r else '-'})")
            except D.KISError as e:
                print(f"  ❌ 순위 조회 실패: {e}")
            try:
                m = D.load_master(log=lambda x: print(x, end=""))
                print(f"  ✅ 종목 마스터 {len(m):,}개")
            except Exception as e:
                print(f"  ❌ 종목 마스터 다운로드 실패: {e} (순위 종목만으로 스캔됨)")
            print(f"  DART: {'연결됨' if D._DART else '키 없음 (공시 차단 꺼짐)'}")
        self._run_engine("연결 점검", job)

    def review(self):
        if not RECORD_XLSX.exists():
            messagebox.showinfo("자동 채점", "아직 기록이 없어. '엑셀 기록'을 켜고 며칠 스캔한 뒤에 눌러줘."); return
        self.log(f"\n[{datetime.now():%H:%M:%S}] 추천 자동 채점 (매매일지 기준)\n")
        self._run_engine("추천 자동 채점", lambda: D.run_review(RECORD_XLSX))

    def check_holding(self):
        spec = self.v_hold.get().replace(" ", "")
        if ":" not in spec:
            messagebox.showinfo("보유 점검", "형식: 종목명(또는 코드):매수가\n예) 휴림로봇:6500,005930:75000"); return
        p = self._params()
        if not p:
            return
        self._save_settings()
        self.log(f"\n[{datetime.now():%H:%M:%S}] 보유 종목 점검\n")

        def job():
            D.init_feeds()
            try:
                names = {r["name"].replace(" ", ""): r["code"] for r in D.load_master(log=lambda x: None)}
            except Exception:
                names = {}
            today = datetime.now().strftime("%Y%m%d")
            for item in spec.split(","):
                if ":" not in item:
                    continue
                nm, bp = item.split(":", 1)
                code = D.norm_code(nm) if len(nm) == 6 and nm.isalnum() else names.get(nm)
                try:
                    buy = float(bp.replace(",", ""))
                except ValueError:
                    buy = 0
                if not code or buy <= 0:
                    print(f"  ❌ {nm}: 종목명을 못 찾음 (6자리 코드로 넣어봐) 또는 매수가 오류"); continue
                rt = D.fetch_realtime(code)
                hist = [d for d in (D.fetch_daily(code) or []) if d["date"] < today]
                pnl = (rt["price"] / buy - 1) * 100
                pl0 = pl = D.trade_plan(buy, hist, p["stop"])
                if rt["price"] <= pl0["stop"]:
                    act = f"🔴 손절가({pl0['stop']:,}) 이탈 — 전량 손절 원칙"
                elif rt["price"] >= pl0["target"]:
                    act = f"✅ 목표가(+{pl0['target_pct']:.1f}%) 도달 — 계획대로 매도"
                elif pnl <= -pl0["stop_pct"] * 0.7:
                    act = "⚠️ 손절선 근접"
                else:
                    act = "보유"
                print(f"  {nm}  매수 {buy:,.0f} → 현재 {rt['price']:,} ({pnl:+.1f}%)  {act}")
                print(f"      매수가 기준 손절 {pl['stop']:,} (-{pl['stop_pct']:.1f}%) / 목표 {pl['target']:,} "
                      f"(+{pl['target_pct']:.1f}%)  ATR {fmt_f(pl.get('atr_pct'), 1)}%")
                sc = D.calc_score_v15(hist, rt, {}, False, None, D._safe(D.fetch_filings, code, []), None)
                flags = [b for b in sc["blocks"] if not b.startswith("갭업")] + \
                        [w for w in sc["warnings"] if ("음봉" in w or "VWAP" in w or "하락" in w)]
                if flags:
                    print(f"      ⚠️ 위험 신호: {' | '.join(flags[:4])}")
        self._run_engine("보유 점검", job)

    def save_csv(self):
        if not self.results:
            messagebox.showinfo("CSV 저장", "먼저 스캔을 실행해줘."); return
        path = filedialog.asksaveasfilename(title="결과 저장", initialdir=str(WORK_DIR), defaultextension=".csv",
                                            initialfile=f"단타스캔_{datetime.now():%Y%m%d_%H%M}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["판정", "종목명", "코드", "섹터", "점수", "현재가", "등락%", "거래량배", "VWAP", "체결강도",
                        "시총(억)", "ATR%", "손절가", "목표가", "행동", "매수근거", "경고", "차단사유", "선도제외사유"])
            for r in self.results:
                w.writerow([r["grade"], r["name"], r["code"], r["sector"], r["score"], r["price"],
                            fmt_f(r["change_rate"], 2), r["vol_accel"], fmt_int(r["vwap"]), fmt_f(r["strength"], 0),
                            r["mcap"], r.get("atr_pct"), r["stop"], r.get("target"), r["action"], " | ".join(r["signals"]),
                            " | ".join(r["warnings"]), " / ".join(r["blocks"]), " / ".join(r.get("lead_block") or [])])
        self.log(f"CSV 저장: {path}\n")

    # ─────────────────────────── 화면 갱신 ───────────────────────────
    def _fill(self, results, portfolio, regime, t_label):
        self.results, self.portfolio, self.regime = results, portfolio, regime
        for tv in (self.tv_cand, self.tv_block, self.tv_port):
            tv.delete(*tv.get_children())
        n_c = n_b = 0
        for r in results:
            tag = GRADE_TAG.get(r["grade"], "")
            if r["grade"] == "⛔차단":
                n_b += 1
                self.tv_block.insert("", "end", iid=f"b{r['code']}", tags=(tag,), values=(
                    r["name"], r["code"], r["sector"], fmt_int(r["price"]), fmt_f(r["change_rate"], 2),
                    " / ".join(r["blocks"])))
            elif r["grade"] != "⬇제외":
                n_c += 1
                self.tv_cand.insert("", "end", iid=f"c{r['code']}", tags=(tag,), values=(
                    r["grade"], r["name"], r["code"], r["sector"], fmt_f(r["score"], 0), fmt_int(r["price"]),
                    fmt_f(r["change_rate"], 2), fmt_f(r["vol_accel"], 1), fmt_int(r["vwap"]),
                    fmt_f(r["strength"], 0), fmt_int(r["mcap"]), fmt_f(r.get("atr_pct"), 1), fmt_int(r["stop"]),
                    fmt_int(r.get("target")), r["action"], " / ".join(r.get("lead_block") or []),
                    " | ".join(r["signals"][:4])))
        for r in portfolio:
            self.tv_port.insert("", "end", iid=f"p{r['code']}", tags=(GRADE_TAG.get(r["grade"], ""),), values=(
                r["name"], r["sector"], r["grade"], fmt_f(r["score"], 0), fmt_int(r["price"]), fmt_int(r["shares"]),
                fmt_int(r["invested"]), fmt_int(r["stop_price"]), fmt_int(r["target_price"]),
                fmt_f(r["stop_pct"], 1), fmt_f(r["target_pct"], 1), fmt_f(r.get("atr_pct"), 1), r.get("basis", ""),
                fmt_int(r["max_loss"]), fmt_int(r["max_gain"])))
        lead = sum(r["grade"] == "🚀선도매수" for r in results)
        self.nb.tab(0, text=f"🎯 매수 후보 ({n_c}, 선도 {lead})")
        self.nb.tab(1, text=f"💰 오늘의 추천 포트폴리오 ({len(portfolio)})")
        self.nb.tab(2, text=f"⛔ 차단 종목 ({n_b})")
        th = regime.get("thresholds", {})
        gate = (regime.get("gate") or {}).get("label", "")
        self.v_regime.set(f"시장 레짐: {regime.get('label', '-')} {gate}   |   등급 기준 선도≥{th.get('선도매수', '-'):.0f} "
                          f"대기≥{th.get('매수대기', '-'):.0f} 관심≥{th.get('관심', '-'):.0f}   |   시간대: {t_label}")
        self.v_status.set(f"완료 {datetime.now():%H:%M} — 후보 {n_c} · 선도 {lead} · 차단 {n_b}")
        if portfolio:
            tot = sum(r["invested"] for r in portfolio)
            loss = sum(r["max_loss"] for r in portfolio)
            cap = float(self.vars["capital"].get().replace(",", "") or 1)
            sp = sum(r["stop_pct"] for r in portfolio) / len(portfolio)
            tp = sum(r["target_pct"] for r in portfolio) / len(portfolio)
            be = (sp + D.TOTAL_FRICTION_PCT) / (sp + tp) * 100
            self.log(f"  💰 추천 {len(portfolio)}종목 / 투입 {tot:,.0f}원 / 전부 손절 시 -{loss:,.0f}원 "
                     f"(자본의 -{loss / cap * 100:.1f}%)\n")
            self.log(f"     평균 손절 -{sp:.1f}% · 목표 +{tp:.1f}% → 비용·슬리피지({D.TOTAL_FRICTION_PCT:.2f}%) 포함 "
                     f"손익분기 승률 약 {be:.0f}%\n")
            if loss / cap * 100 > D.DAILY_LOSS_LIMIT_PCT:
                self.log(f"  ⚠️ 전부 손절 시 손실이 일일 한도({D.DAILY_LOSS_LIMIT_PCT:.0f}%)를 넘음 — 종목 수를 줄여\n")
            self.log("  👉 포트폴리오 탭의 '손절가(자동주문)'을 NH 시세포착 자동주문에 그대로 입력 (호가단위 맞춰 둠)\n")
        else:
            self.log("  ℹ️ 조건 충족 종목 없음 — 오늘은 현금 보유가 전략\n")

    def _on_select(self, event):
        tv = event.widget
        sel = tv.selection()
        if not sel:
            return
        code = sel[0][1:]
        r = next((x for x in self.results if x["code"] == code), None)
        if not r:
            return
        bd = "  ".join(f"{k} {v:+.0f}" for k, v in (r.get("bd") or {}).items())
        lines = [f"{r['name']} ({r['code']}) · {r['sector']} · {r['grade']} {r['score']:.0f}점 · "
                 f"현재가 {fmt_int(r['price'])} ({fmt_f(r['change_rate'], 2)}%) · 20일선 {fmt_int(r.get('ma20'))} · "
                 f"60일선 {fmt_int(r.get('ma60'))} · 손절가 {fmt_int(r['stop'])}"]
        pl = r.get("plan") or {}
        if pl:
            lines.append(f"📐 매매계획: 손절 {fmt_int(pl.get('stop'))} (-{fmt_f(pl.get('stop_pct'), 1)}%, {pl.get('basis')}) · "
                         f"목표 {fmt_int(pl.get('target'))} (+{fmt_f(pl.get('target_pct'), 1)}%) · ATR {fmt_f(pl.get('atr_pct'), 1)}%")
        if r["blocks"]:
            lines.append("⛔ 차단: " + " / ".join(r["blocks"]))
        if r.get("lead_block"):
            lines.append("🚫 진입 제외 사유: " + " / ".join(r["lead_block"]))
        if bd:
            lines.append("점수: " + bd)
        if r["signals"]:
            lines.append("✅ 근거: " + " | ".join(r["signals"]))
        if r["warnings"]:
            lines.append("⚠️ 경고: " + " | ".join(r["warnings"]))
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0", "\n".join(lines))

    def _sort(self, tv, col, desc):
        data = [(tv.set(k, col), k) for k in tv.get_children("")]

        def key(x):
            s = x[0].replace(",", "").replace("%", "")
            try:
                return (0, float(s))
            except ValueError:
                return (1, s)
        data.sort(key=key, reverse=desc)
        for i, (_, k) in enumerate(data):
            tv.move(k, "", i)
        tv.heading(col, command=lambda: self._sort(tv, col, not desc))

    def log(self, s):
        self.txt_log.insert("end", s)
        self.txt_log.see("end")

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log(val)
                elif kind == "max":
                    self.progress.configure(maximum=max(1, val))
                elif kind == "progress":
                    self.progress["value"] = val[0]; self.v_status.set(val[1])
                elif kind == "result":
                    self._fill(*val)
                elif kind == "error":
                    self.log("\n[오류] " + val + "\n")
                    messagebox.showerror("오류", val[-800:])
                elif kind == "idle":
                    self.v_status.set(f"{val} 완료")
                elif kind == "done":
                    self.btn_start.configure(state="normal"); self.btn_stop.configure(state="disabled")
                    self._schedule_repeat()
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _schedule_repeat(self):
        try:
            m = float(self.vars["repeat_min"].get())
        except ValueError:
            m = 0
        if m > 0 and not self.stop_event.is_set():
            if D.market_status() == "AFTER":
                self.log("  장 마감 — 자동 반복 종료\n"); return
            nxt = datetime.now() + timedelta(minutes=m)
            self.log(f"  ⏱ 다음 자동 스캔 {nxt:%H:%M}\n")
            self.repeat_job = self.after(int(m * 60000), lambda: self.start_scan(auto=True))

    def _tick_clock(self):
        label, desc = D.current_time_strategy()
        self.v_clock.set(f"⏰ {datetime.now():%H:%M:%S}   {label} — {desc}")
        self.after(1000, self._tick_clock)

    def _on_close(self):
        self._save_settings()
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
