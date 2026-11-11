# -*- coding: utf-8 -*-
"""
대장주 단기스윙 스캐너 — 독립 실행 GUI
════════════════════════════════════════
swing_v1.py(채점 엔진)를 창 프로그램으로 감싼 버전.
단타 스캐너(DantaScanner)와 완전히 별개로 돌고, KIS 키/캐시는 같은 폴더를 공유한다.

같은 폴더에 필요: swing_v1.py, danta_v16.py, kis_config.json
실행: run_swing.bat 더블클릭 (또는 pythonw SwingScanner_GUI.py)
"""
import contextlib
import csv
import io
import json
import queue
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
WORK_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else APP_DIR
sys.path.insert(0, str(APP_DIR))

import danta_v16 as D          # noqa: E402  (피더)
import swing_v1 as S           # noqa: E402  (스윙 엔진)

# exe로 묶었을 때도 설정·캐시·엑셀은 exe 옆 폴더를 쓰도록
D.SCRIPT_DIR = WORK_DIR
D.CONFIG_PATH = WORK_DIR / "kis_config.json"
D.TOKEN_PATH = WORK_DIR / ".kis_token.json"
D.CACHE_DIR = WORK_DIR / "cache_v16"
S.SCRIPT_DIR = WORK_DIR

SETTINGS_PATH = WORK_DIR / "swing_settings.json"
FONT = ("맑은 고딕", 9)
FONT_B = ("맑은 고딕", 9, "bold")

DEFAULTS = {
    "capital": "9000000", "positions": "3", "stop": "5", "risk": "1",
    "mcap_min": "10000", "mcap_max": "0", "min_amount": "50", "top_n": "120",
    "th_lead": "55", "th_wait": "42", "th_watch": "28", "write_excel": True,
}

CAND_COLS = [("grade", "판정", 95), ("name", "종목명", 120), ("code", "코드", 60), ("sector", "섹터", 90),
             ("score", "점수", 48), ("price", "현재가", 80), ("chg", "등락%", 58), ("pullback", "눌림%", 58),
             ("vol", "거래량배", 62), ("fore", "외인3일", 78), ("inst", "기관3일", 78), ("atr", "ATR%", 52),
             ("stop", "손절가", 78), ("target", "목표가", 78), ("note", "선도 제외 사유", 170),
             ("signal", "매수 근거", 320)]
PORT_COLS = [("name", "종목명", 120), ("sector", "섹터", 90), ("grade", "판정", 95), ("score", "점수", 48),
             ("price", "현재가", 80), ("shares", "수량", 55), ("invested", "투입금액", 95),
             ("stop_price", "손절가(자동주문)", 115), ("target_price", "목표가(자동주문)", 115),
             ("stop_pct", "손절%", 55), ("target_pct", "목표%", 55), ("atr_pct", "ATR%", 52),
             ("max_loss", "최대손실", 90), ("max_gain", "최대이익", 90)]
BLOCK_COLS = [("name", "종목명", 120), ("code", "코드", 60), ("sector", "섹터", 90), ("price", "현재가", 80),
              ("chg", "등락%", 58), ("reason", "차단 사유", 620)]

GRADE_TAG = {"🚀선도매수": "lead", "⬆매수대기": "wait", "👀관심": "watch", "⬇제외": "out", "⛔차단": "block"}


class QueueWriter(io.TextIOBase):
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
        self.title("대장주 단기스윙 스캐너 — KIS 실시간")
        self.geometry("1480x900")
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
        self.log("대장주 단기스윙 스캐너 준비 완료. KIS 키 입력 → [키 저장] → [연결 점검] → [▶ 스캔 시작].\n"
                 "대상은 시총 상위 대형주를 KIS 마스터에서 자동 선정. 일봉 200개씩 조회해서 수 분 걸릴 수 있어.\n"
                 "단타 스캐너(DantaScanner)와 완전히 별개 프로그램이고, 키·캐시만 같은 폴더를 공유해.\n")

    # ─────────────── UI ───────────────
    def _build_keys(self):
        f = ttk.LabelFrame(self, text="KIS API 키 (단타 스캐너와 같은 kis_config.json 공유)")
        f.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(f, text="APP KEY").grid(row=0, column=0, padx=(8, 4), pady=6, sticky="w")
        self.v_key = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_key, show="•", width=40).grid(row=0, column=1, padx=4)
        ttk.Label(f, text="APP SECRET").grid(row=0, column=2, padx=(14, 4))
        self.v_secret = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_secret, show="•", width=46).grid(row=0, column=3, padx=4)
        ttk.Label(f, text="DART 키(선택)").grid(row=0, column=4, padx=(14, 4))
        self.v_dart = tk.StringVar()
        ttk.Entry(f, textvariable=self.v_dart, show="•", width=26).grid(row=0, column=5, padx=4)
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
        field(0, 8, "top_n", "스캔 상위(시총)", 6)
        field(1, 0, "mcap_min", "시총 하한(억)", 8)
        field(1, 2, "mcap_max", "시총 상한(억, 0=무제한)", 8)
        field(1, 4, "min_amount", "최소 거래대금(억)", 6)
        field(1, 6, "th_lead", "🚀선도 ≥", 5)
        field(1, 8, "th_wait", "⬆대기 ≥", 5)
        field(1, 10, "th_watch", "👀관심 ≥", 5)
        self.v_excel = tk.BooleanVar(value=bool(self.settings.get("write_excel", True)))
        ttk.Checkbutton(f, text="엑셀 기록 (대장주스윙_기록.xlsx)", variable=self.v_excel).grid(
            row=0, column=10, columnspan=2, padx=8, sticky="w")

    def _build_controls(self):
        f = ttk.Frame(self)
        f.pack(fill="x", padx=8, pady=4)
        self.btn_start = ttk.Button(f, text="▶ 스캔 시작", command=self.start_scan, width=12)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(f, text="■ 중단", command=self.stop_scan, state="disabled", width=8)
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(f, text="결과 CSV 저장", command=self.save_csv).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(f, mode="determinate", style="green.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.v_status = tk.StringVar(value="대기")
        ttk.Label(f, textvariable=self.v_status, width=40).pack(side="left")

        g = ttk.Frame(self)
        g.pack(fill="x", padx=8)
        self.v_clock = tk.StringVar()
        self.v_regime = tk.StringVar(value="지수 게이트: -")
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
        self.nb.add(f1, text="🎯 스윙 후보")
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

    # ─────────────── 설정/키 ───────────────
    def _load_settings(self):
        try:
            return {**DEFAULTS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
        except Exception:
            return dict(DEFAULTS)

    def _save_settings(self):
        s = {k: v.get() for k, v in self.vars.items()}
        s["write_excel"] = self.v_excel.get()
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
        D._KIS = None; D._DART = None
        self.log(f"키 저장 완료 → {D.CONFIG_PATH.name} ({'모의' if self.v_mock.get() else '실전'} 서버)\n")

    # ─────────────── 파라미터 ───────────────
    def _params(self):
        try:
            p = {k: float(v.get().replace(",", "")) for k, v in self.vars.items()}
        except ValueError:
            messagebox.showerror("입력 오류", "발굴 조건에는 숫자만 넣어줘."); return None
        if not (p["th_lead"] > p["th_wait"] > p["th_watch"]):
            messagebox.showerror("입력 오류", "등급 기준은 선도 > 대기 > 관심 순이어야 해."); return None
        p["grade"] = {"🚀선도매수": p["th_lead"], "⬆매수대기": p["th_wait"], "👀관심": p["th_watch"]}
        return p

    # ─────────────── 스캔 ───────────────
    def start_scan(self):
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
        self.v_status.set("중단 요청됨 — 현재 종목까지 처리 후 멈춤")

    def _scan_worker(self, p):
        q = self.q
        try:
            def log(m):
                q.put(("log", m))

            def progress(i, n, name):
                q.put(("progress", (i, n, f"조회 {i}/{n} — {name}")))

            with contextlib.redirect_stdout(QueueWriter(q)):
                results, portfolio, regime = S.run_scan(
                    p, log=log, progress=progress,
                    should_stop=self.stop_event.is_set, write_excel=self.v_excel.get())
            q.put(("result", (results, portfolio, regime)))
        except D.KISError as e:
            q.put(("error", f"KIS 연결 실패: {e}\n\n키와 실전/모의 설정을 확인해줘."))
        except Exception:
            q.put(("error", traceback.format_exc()))
        finally:
            q.put(("done", None))

    def check_connection(self):
        self.log(f"\n[{datetime.now():%H:%M:%S}] 연결 점검\n")
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("연결 점검", "스캔이 끝난 뒤에 실행해줘."); return

        def job():
            try:
                with contextlib.redirect_stdout(QueueWriter(self.q)):
                    D.init_feeds()
                    rt = D.fetch_realtime("005930")
                    print(f"  ✅ 현재가: 삼성전자 {rt['price']:,}원 ({rt['change_rate']:+.2f}%) 업종 {rt.get('sector')}")
                    d = S.fetch_daily_deep("005930")
                    print(f"  ✅ 일봉 {len(d)}개 (200일선 {'가능' if len(d) >= 200 else '부족→120일선 사용'})")
                    print(f"  {'✅' if D._safe(D.fetch_investor, '005930', {}) else '❌'} 투자자 수급 조회")
                    print(f"  DART: {'연결됨' if D._DART else '키 없음 (공시 차단 꺼짐)'}")
            except Exception:
                self.q.put(("error", traceback.format_exc()))
            finally:
                self.q.put(("idle", "연결 점검"))
        self.v_status.set("연결 점검 중...")
        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def save_csv(self):
        if not self.results:
            messagebox.showinfo("CSV 저장", "먼저 스캔을 실행해줘."); return
        path = filedialog.asksaveasfilename(title="결과 저장", initialdir=str(WORK_DIR), defaultextension=".csv",
                                            initialfile=f"대장주스윙_{datetime.now():%Y%m%d_%H%M}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp)
            w.writerow(["판정", "종목명", "코드", "섹터", "점수", "현재가", "등락%", "눌림%", "거래량배",
                        "외인3일", "기관3일", "ATR%", "손절가", "목표가", "선도제외", "매수근거", "경고", "차단사유"])
            for r in self.results:
                w.writerow([r["grade"], r["name"], r["code"], r["sector"], round(r["score"]), r["price"],
                            fmt_f(r["change_rate"], 2), r["pullback"], r["vol_ratio"], r["fore_3d"], r["inst_3d"],
                            r.get("atr_pct"), r["stop"], r.get("target"), " / ".join(r.get("lead_block") or []),
                            " | ".join(r["signals"]), " | ".join(r["warnings"]), " / ".join(r["blocks"])])
        self.log(f"CSV 저장: {path}\n")

    # ─────────────── 화면 ───────────────
    def _fill(self, results, portfolio, regime):
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
                    fmt_f(r["change_rate"], 2), fmt_f(r["pullback"], 1), fmt_f(r["vol_ratio"], 1),
                    fmt_int(r["fore_3d"]), fmt_int(r["inst_3d"]), fmt_f(r.get("atr_pct"), 1), fmt_int(r["stop"]),
                    fmt_int(r.get("target")), " / ".join(r.get("lead_block") or []), " | ".join(r["signals"][:4])))
        for r in portfolio:
            self.tv_port.insert("", "end", iid=f"p{r['code']}", tags=(GRADE_TAG.get(r["grade"], ""),), values=(
                r["name"], r["sector"], r["grade"], fmt_f(r["score"], 0), fmt_int(r["price"]), fmt_int(r["shares"]),
                fmt_int(r["invested"]), fmt_int(r["stop_price"]), fmt_int(r["target_price"]),
                fmt_f(r["stop_pct"], 1), fmt_f(r["target_pct"], 1), fmt_f(r.get("atr_pct"), 1),
                fmt_int(r["max_loss"]), fmt_int(r["max_gain"])))
        lead = sum(r["grade"] == "🚀선도매수" for r in results)
        self.nb.tab(0, text=f"🎯 스윙 후보 ({n_c}, 선도 {lead})")
        self.nb.tab(1, text=f"💰 추천 포트폴리오 ({len(portfolio)})")
        self.nb.tab(2, text=f"⛔ 차단 종목 ({n_b})")
        gate = (regime.get("gate") or {}).get("desc", "")
        self.v_regime.set(f"지수 게이트: {regime.get('label', '-')}   |   {gate}")
        self.v_status.set(f"완료 {datetime.now():%H:%M} — 후보 {n_c} · 선도 {lead} · 차단 {n_b}")
        if portfolio:
            tot = sum(r["invested"] for r in portfolio)
            loss = sum(r["max_loss"] for r in portfolio)
            cap = float(self.vars["capital"].get().replace(",", "") or 1)
            self.log(f"  💰 추천 {len(portfolio)}종목 / 투입 {tot:,.0f}원 / 전부 손절 시 -{loss:,.0f}원 "
                     f"(자본의 -{loss / cap * 100:.1f}%)\n")
            self.log("  👉 '손절가(자동주문)'을 NH 시세포착 자동주문에 그대로 입력 (호가단위 맞춰 둠)\n")
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
                 f"60일선 {fmt_int(r.get('ma60'))} · 120일선 {fmt_int(r.get('ma120'))}"]
        pl = r.get("plan") or {}
        if pl:
            lines.append(f"📐 매매계획: 손절 {fmt_int(pl.get('stop'))} (-{fmt_f(pl.get('stop_pct'), 1)}%, {pl.get('basis')}) · "
                         f"목표 {fmt_int(pl.get('target'))} (+{fmt_f(pl.get('target_pct'), 1)}%) · ATR {fmt_f(pl.get('atr_pct'), 1)}%")
        if r["blocks"]:
            lines.append("⛔ 차단: " + " / ".join(r["blocks"]))
        if r.get("lead_block"):
            lines.append("🚫 선도 제외: " + " / ".join(r["lead_block"]))
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
                elif kind == "progress":
                    i, n, msg = val
                    self.progress.configure(maximum=max(1, n))
                    self.progress["value"] = i
                    self.v_status.set(msg)
                elif kind == "result":
                    self._fill(*val)
                elif kind == "error":
                    self.log("\n[오류] " + val + "\n")
                    messagebox.showerror("오류", val[-800:])
                elif kind == "idle":
                    self.v_status.set(f"{val} 완료")
                    self.btn_start.configure(state="normal"); self.btn_stop.configure(state="disabled")
                elif kind == "done":
                    self.btn_start.configure(state="normal"); self.btn_stop.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _tick_clock(self):
        self.v_clock.set(f"⏰ {datetime.now():%H:%M:%S}   {D.market_status()}")
        self.after(1000, self._tick_clock)

    def _on_close(self):
        self._save_settings()
        self.stop_event.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
