# -*- coding: utf-8 -*-
"""
gui.py — 대장주 자동 발굴기 데스크톱 GUI (Tkinter)

auto_finder.py 의 스캔 로직을 그대로 사용하고, 백그라운드 스레드에서 돌려
진행 상황/로그/결과를 화면에 표시한다.

실행: python gui.py
준비: pip install -r requirements.txt
"""
from __future__ import annotations

import csv
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import kis_feed as kf
import dart_feed as dart
import auto_finder as af

_SETTINGS_FILE = Path(__file__).with_name("gui_settings.json")   # 조건값만 저장 (API 키는 저장하지 않음)


class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("대장주 자동 발굴기")
        self.geometry("1100x780")
        self.minsize(900, 600)

        self._worker = None
        self._stop_requested = False
        self._msg_queue: "queue.Queue" = queue.Queue()
        self._last_result: af.ScanResult | None = None

        self._build_widgets()
        self._load_saved_keys()
        self._load_settings()
        self.after(100, self._poll_queue)

    # ── UI 구성 ──────────────────────────────────────────────
    def _build_widgets(self):
        pad = {"padx": 6, "pady": 3}

        # 상단: API 키
        key_frame = ttk.LabelFrame(self, text="API 키")
        key_frame.pack(fill="x", **pad)

        ttk.Label(key_frame, text="KIS APP KEY").grid(row=0, column=0, sticky="e", **pad)
        self.var_app_key = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_key, width=42, show="•").grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(key_frame, text="APP SECRET").grid(row=0, column=2, sticky="e", **pad)
        self.var_app_secret = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_secret, width=42, show="•").grid(row=0, column=3, sticky="w", **pad)

        self.var_virtual = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="모의투자 서버 사용", variable=self.var_virtual).grid(row=0, column=4, sticky="w", **pad)
        ttk.Button(key_frame, text="키 저장", command=self._save_keys).grid(row=0, column=5, sticky="w", **pad)

        ttk.Label(key_frame, text="DART API KEY").grid(row=1, column=0, sticky="e", **pad)
        self.var_dart_key = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_dart_key, width=42, show="•").grid(row=1, column=1, sticky="w", **pad)
        self.var_use_dart = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="DART 재무정보 사용 (느려짐)", variable=self.var_use_dart).grid(
            row=1, column=2, columnspan=2, sticky="w", **pad)
        ttk.Button(key_frame, text="DART 키 저장", command=self._save_dart_key).grid(row=1, column=5, sticky="w", **pad)

        # 조건 설정
        cond_frame = ttk.LabelFrame(self, text="발굴 조건")
        cond_frame.pack(fill="x", **pad)

        self.var_top_n = tk.IntVar(value=af.TOP_N)
        self.var_min_mcap = tk.IntVar(value=af.MIN_MCAP_억)
        self.var_pullback = tk.DoubleVar(value=af.PULLBACK_PCT)
        self.var_max_pullback = tk.DoubleVar(value=af.MAX_PULLBACK_PCT)
        self.var_lookback = tk.IntVar(value=af.LOOKBACK)
        self.var_ma = tk.IntVar(value=af.MA_PERIOD)
        self.var_max_show = tk.IntVar(value=af.MAX_SHOW)
        self.var_min_trade_value = tk.DoubleVar(value=af.MIN_AVG_TRADE_VALUE_억)
        self.var_max_per = tk.DoubleVar(value=af.MAX_PER)
        self.var_max_pbr = tk.DoubleVar(value=af.MAX_PBR)
        self.var_max_debt_ratio = tk.DoubleVar(value=af.MAX_DEBT_RATIO)
        self.var_require_op_profit = tk.BooleanVar(value=af.REQUIRE_OP_PROFIT)
        self.var_require_ma_rising = tk.BooleanVar(value=af.REQUIRE_MA_RISING)
        self.var_use_flow = tk.BooleanVar(value=af.USE_FLOW)

        # (라벨, 변수, 입력폭, 표시이름) — 입력 검증 때 표시이름으로 어느 칸이 문제인지 알려준다
        self._num_fields = [
            ("시총 상위 N개", self.var_top_n, 8, int),
            ("최소 시총(억)", self.var_min_mcap, 10, int),
            ("눌림 기준(%)", self.var_pullback, 8, float),
            ("눌림 상한(%, 0=미사용)", self.var_max_pullback, 8, float),
            ("고점 산정일", self.var_lookback, 8, int),
            ("이동평균일", self.var_ma, 8, int),
            ("결과 최대표시", self.var_max_show, 8, int),
            ("최소 평균거래대금(억)", self.var_min_trade_value, 8, float),
            ("PER 상한(0=미사용)", self.var_max_per, 8, float),
            ("PBR 상한(0=미사용)", self.var_max_pbr, 8, float),
            ("[DART] 부채비율 상한%(0=미사용)", self.var_max_debt_ratio, 8, float),
        ]
        per_row = 4
        for i, (label, var, width, _cast) in enumerate(self._num_fields):
            r, col = divmod(i, per_row)
            ttk.Label(cond_frame, text=label).grid(row=r, column=col * 2, sticky="e", **pad)
            ttk.Entry(cond_frame, textvariable=var, width=width).grid(row=r, column=col * 2 + 1, sticky="w", **pad)

        opt_row = (len(self._num_fields) + per_row - 1) // per_row
        ttk.Checkbutton(cond_frame, text="[DART] 영업이익 흑자 종목만", variable=self.var_require_op_profit).grid(
            row=opt_row, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(cond_frame, text="200일선 상승 중인 종목만", variable=self.var_require_ma_rising).grid(
            row=opt_row, column=2, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(cond_frame, text="추천 후보 수급(외국인/기관 5일) 확인", variable=self.var_use_flow).grid(
            row=opt_row, column=4, columnspan=4, sticky="w", **pad)

        # 실행 버튼 + 진행바
        run_frame = ttk.Frame(self)
        run_frame.pack(fill="x", **pad)

        self.btn_run = ttk.Button(run_frame, text="▶ 스캔 시작", command=self._start_scan)
        self.btn_run.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(run_frame, text="■ 중단", command=self._stop_scan, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_export = ttk.Button(run_frame, text="결과 CSV 저장", command=self._export_csv, state="disabled")
        self.btn_export.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(run_frame, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_progress = ttk.Label(run_frame, text="대기 중")
        self.lbl_progress.pack(side="left", padx=6)

        self.lbl_notice = ttk.Label(self, text="", foreground="#b45309")
        self.lbl_notice.pack(fill="x", padx=8)

        # 결과: 추천 3종목 / 조건 충족 / 칼날 주의 를 위아래로 같이 표시
        result_frame = ttk.Frame(self)
        result_frame.pack(fill="both", expand=True, **pad)
        result_frame.rowconfigure(0, weight=2)
        result_frame.rowconfigure(1, weight=4)
        result_frame.rowconfigure(2, weight=3)
        result_frame.rowconfigure(3, weight=2)
        result_frame.columnconfigure(0, weight=1)

        picks_box = ttk.LabelFrame(result_frame, text="⭐ 추천 후보 3종목 — 조건 점수 기준(매수 권유 아님)")
        picks_box.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        self.tree_picks = self._make_pick_tree(picks_box, height=3)

        hits_box = ttk.LabelFrame(result_frame, text="🟢 조건 충족 — 대형주 + 상승추세 + 눌림")
        hits_box.grid(row=1, column=0, sticky="nsew", pady=4)
        self.tree_hits = self._make_tree(hits_box, height=8)

        knives_box = ttk.LabelFrame(result_frame, text="⚠️ 칼날 주의 — 눌림은 왔지만 하락추세(200일선 아래)")
        knives_box.grid(row=2, column=0, sticky="nsew", pady=4)
        self.tree_knives = self._make_tree(knives_box, height=5)

        bounce_box = ttk.LabelFrame(
            result_frame,
            text="⚡ 단타 반등 후보 — 칼날주의 중(여전히 하락추세, 초단기·고위험, 매수 권유 아님)",
        )
        bounce_box.grid(row=3, column=0, sticky="nsew", pady=(4, 0))
        self.tree_bounce = self._make_pick_tree(bounce_box, height=3)

        # 로그
        log_frame = ttk.LabelFrame(self, text="로그")
        log_frame.pack(fill="both", **pad)
        self.txt_log = tk.Text(log_frame, height=6, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

    _TREE_COLS = ("name", "code", "sector", "mcap", "price", "pullback", "above_ma", "ma_slope", "ma_confirm",
                  "trade_value", "vol_ratio", "per", "pbr", "per_rel", "debt_ratio", "op_yoy", "rev_yoy",
                  "op_positive", "fin_period", "flow_f", "flow_o", "score", "is_new")
    _TREE_HEADERS = {
        "name": "종목명", "code": "코드", "sector": "업종", "mcap": "시총(억)",
        "price": "현재가", "pullback": "눌림(%)", "above_ma": "200일선위(%)", "ma_slope": "200일선기울기(%)",
        "ma_confirm": "최근5일중MA위", "trade_value": "평균거래대금(억)", "vol_ratio": "거래량비(배)",
        "per": "PER", "pbr": "PBR", "per_rel": "업종PER대비(%)",
        "debt_ratio": "부채비율(%)", "op_yoy": "영업이익YoY(%)", "rev_yoy": "매출YoY(%)", "op_positive": "영업흑자",
        "fin_period": "재무 기준", "flow_f": "외국인5일", "flow_o": "기관5일", "score": "점수", "is_new": "변동",
    }

    def _scrolled(self, parent, tree):
        vsb = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

    def _make_tree(self, parent, height=10):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=self._TREE_COLS, show="headings", height=height)
        for c in self._TREE_COLS:
            tree.heading(c, text=self._TREE_HEADERS[c])
            tree.column(c, width=100, anchor="center", stretch=False)
        tree.column("name", width=150, anchor="w")
        tree.column("sector", width=90, anchor="w")
        tree.column("fin_period", width=120)
        self._scrolled(frame, tree)
        return tree

    def _make_pick_tree(self, parent, height=3):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        cols = ("rank", "name", "code", "sector", "price", "pullback", "above_ma", "score", "reasons")
        headers = {"rank": "순위", "name": "종목명", "code": "코드", "sector": "업종", "price": "현재가",
                   "pullback": "눌림(%)", "above_ma": "200일선위(%)", "score": "점수", "reasons": "선정 이유"}
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=height)
        for c in cols:
            tree.heading(c, text=headers[c])
            tree.column(c, width=90, anchor="center", stretch=False)
        tree.column("rank", width=45)
        tree.column("name", width=150, anchor="w")
        tree.column("sector", width=90, anchor="w")
        tree.column("reasons", width=560, anchor="w")
        self._scrolled(frame, tree)
        return tree

    # ── 키 저장/로딩 ─────────────────────────────────────────
    def _load_saved_keys(self):
        cfg = kf._read_key_file()
        if cfg:
            self.var_app_key.set(cfg.get("app_key", ""))
            self.var_app_secret.set(cfg.get("app_secret", ""))
            self.var_virtual.set(bool(cfg.get("is_virtual", False)))
        dart_cfg = dart._read_key_file()
        if dart_cfg:
            self.var_dart_key.set(dart_cfg.get("api_key", ""))

    def _save_keys(self):
        app_key = self.var_app_key.get().strip()
        app_secret = self.var_app_secret.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("입력 필요", "APP KEY / APP SECRET 을 입력하세요.")
            return
        kf.set_keys(app_key, app_secret, self.var_virtual.get(), save=True)
        messagebox.showinfo("저장 완료", "kis_keys.json 에 저장했습니다.")

    def _save_dart_key(self):
        api_key = self.var_dart_key.get().strip()
        if not api_key:
            messagebox.showwarning("입력 필요", "DART API KEY를 입력하세요.")
            return
        dart.set_key(api_key, save=True)
        messagebox.showinfo("저장 완료", "dart_key.json 에 저장했습니다.")

    # ── 조건값 저장/로딩 (마지막 사용값 유지) ────────────────────
    def _settings_vars(self):
        d = {f"num{i}": f[1] for i, f in enumerate(self._num_fields)}
        d.update(use_dart=self.var_use_dart, op_profit=self.var_require_op_profit,
                 ma_rising=self.var_require_ma_rising, use_flow=self.var_use_flow, virtual=self.var_virtual)
        return d

    def _load_settings(self):
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for k, var in self._settings_vars().items():
            if k in data:
                try:
                    var.set(data[k])
                except Exception:
                    pass

    def _save_settings(self):
        out = {}
        for k, var in self._settings_vars().items():
            try:
                out[k] = var.get()
            except Exception:
                pass
        try:
            _SETTINGS_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── 입력 검증 ────────────────────────────────────────────
    def _read_params(self):
        """입력칸을 읽어 run_scan 인자 dict 로. 비었거나 숫자가 아니면 어느 칸인지 알려주는 ValueError."""
        vals = {}
        for label, var, _w, cast in self._num_fields:
            try:
                vals[label] = cast(var.get())
            except (tk.TclError, ValueError):
                raise ValueError(f"'{label}' 칸에 올바른 숫자를 입력하세요. (비어 있거나 숫자가 아닙니다)")

        def g(label):
            return vals[label]

        if g("시총 상위 N개") < 1:
            raise ValueError("'시총 상위 N개'는 1 이상이어야 합니다.")
        if g("고점 산정일") < 2:
            raise ValueError("'고점 산정일'은 2 이상이어야 합니다.")
        if g("이동평균일") < 20:
            raise ValueError("'이동평균일'은 20 이상이어야 합니다.")
        if g("결과 최대표시") < 1:
            raise ValueError("'결과 최대표시'는 1 이상이어야 합니다.")
        for label in ("눌림 기준(%)", "눌림 상한(%, 0=미사용)", "최소 평균거래대금(억)",
                      "PER 상한(0=미사용)", "PBR 상한(0=미사용)", "[DART] 부채비율 상한%(0=미사용)", "최소 시총(억)"):
            if g(label) < 0:
                raise ValueError(f"'{label}'은 0 이상이어야 합니다.")
        if g("눌림 상한(%, 0=미사용)") and g("눌림 상한(%, 0=미사용)") <= g("눌림 기준(%)"):
            raise ValueError("'눌림 상한'은 '눌림 기준'보다 커야 합니다. (미사용이면 0)")

        return dict(
            top_n=g("시총 상위 N개"),
            min_mcap_억=g("최소 시총(억)"),
            pullback_pct=g("눌림 기준(%)"),
            max_pullback_pct=g("눌림 상한(%, 0=미사용)"),
            lookback=g("고점 산정일"),
            ma_period=g("이동평균일"),
            min_avg_trade_value_억=g("최소 평균거래대금(억)"),
            max_per=g("PER 상한(0=미사용)"),
            max_pbr=g("PBR 상한(0=미사용)"),
            max_debt_ratio=g("[DART] 부채비율 상한%(0=미사용)"),
            require_op_profit=bool(self.var_require_op_profit.get()),
            require_ma_rising=bool(self.var_require_ma_rising.get()),
            use_flow=bool(self.var_use_flow.get()),
        )

    # ── 스캔 실행 ────────────────────────────────────────────
    def _start_scan(self):
        if self._worker and self._worker.is_alive():
            return

        app_key = self.var_app_key.get().strip()
        app_secret = self.var_app_secret.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("입력 필요", "APP KEY / APP SECRET 을 입력하세요.")
            return

        try:
            params = self._read_params()
        except ValueError as e:
            messagebox.showwarning("입력 확인", str(e))
            return

        kf.set_keys(app_key, app_secret, self.var_virtual.get())

        use_dart = bool(self.var_use_dart.get())
        if use_dart:
            dart_key = self.var_dart_key.get().strip()
            if not dart_key:
                messagebox.showwarning("입력 필요", "DART 재무정보를 쓰려면 DART API KEY를 입력하세요.")
                return
            dart.set_key(dart_key)
        params["use_dart"] = use_dart

        self._save_settings()

        for tree in (self.tree_picks, self.tree_hits, self.tree_knives, self.tree_bounce):
            for item in tree.get_children():
                tree.delete(item)
        self._set_log("")
        self.lbl_notice.configure(text="")
        self.progress["value"] = 0
        self.btn_export.configure(state="disabled")

        self._stop_requested = False
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_progress.configure(text="시작 중...")

        self._worker = threading.Thread(target=self._run_worker, args=(params,), daemon=True)
        self._worker.start()

    def _stop_scan(self):
        self._stop_requested = True
        self.btn_stop.configure(state="disabled")

    def _run_worker(self, params):
        def progress_cb(done, total, code, name):
            self._msg_queue.put(("progress", done, total, code, name))

        def log_cb(msg):
            self._msg_queue.put(("log", msg))

        def stop_flag():
            return self._stop_requested

        try:
            res = af.run_scan(progress_cb=progress_cb, log_cb=log_cb, stop_flag=stop_flag, **params)
            self._msg_queue.put(("done", res))
        except ImportError:
            self._msg_queue.put(("error", "필요한 패키지가 설치되어 있지 않습니다.\npip install -r requirements.txt"))
        except Exception as e:
            self._msg_queue.put(("error", f"스캔 실패: {e}"))

    # ── 메인스레드 큐 폴링 ───────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                try:
                    item = self._msg_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_message(item)
                except Exception as e:   # 한 건의 처리 오류가 폴링 전체를 멈추지 않게
                    self._append_log(f"[화면 갱신 오류] {e}")
                    self._finish_run()
        finally:
            self.after(100, self._poll_queue)

    def _handle_message(self, item):
        kind = item[0]
        if kind == "progress":
            _, done, total, code, name = item
            self.progress["maximum"] = max(total, 1)
            self.progress["value"] = done
            self.lbl_progress.configure(text=f"{done}/{total}  {name}({code})")
        elif kind == "log":
            self._append_log(item[1])
        elif kind == "done":
            res: af.ScanResult = item[1]
            self._last_result = res
            self._fill_picks(self.tree_picks, res.picks, "눌림 7~20%, 200일선 상승, 거래량 과열 아님, 흑자 등")
            self._fill_tree(self.tree_hits, res.hits)
            self._fill_tree(self.tree_knives, res.knives)
            self._fill_picks(self.tree_bounce, res.bounce_picks,
                              "낙폭 15~45%, 200일선 대비 -3~-35%, 과열(4배↑) 아님, 흑자 등")
            self.btn_export.configure(
                state="normal" if (res.hits or res.knives or res.picks or res.bounce_picks) else "disabled"
            )
            tail = " (중단됨)" if res.stopped else ""
            self.lbl_progress.configure(
                text=f"완료{tail} — 조회 {res.scanned}/{res.universe_len}, 충족 {len(res.hits)}, "
                     f"칼날 {len(res.knives)}, 추천 {len(res.picks)}, 단타후보 {len(res.bounce_picks)}")
            notes = []
            if res.intraday:
                notes.append("⚠ 장중 데이터: 거래량비·거래대금·이동평균은 직전 거래일까지 기준")
            if res.exited:
                notes.append("직전 스캔 대비 이탈: " + ", ".join(res.exited[:6]))
            self.lbl_notice.configure(text="   |   ".join(notes))
            self._finish_run()
        elif kind == "error":
            self._append_log(f"[오류] {item[1]}")
            messagebox.showerror("오류", item[1])
            self._finish_run()

    def _finish_run(self):
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    # ── 표 채우기 ────────────────────────────────────────────
    @staticmethod
    def _cand_values(c: af.Candidate):
        mc = f"{c.mcap:,}" if c.mcap else "-"
        pbr_s = f"{c.pbr:.2f}" if c.pbr is not None else "-"
        rel_s = f"{c.per_rel:+.0f}" if c.per_rel is not None else "-"
        if c.debt_ratio is None:
            debt_s = "-"
        else:
            debt_s = f"{c.debt_ratio:.0f}" + ("(금융)" if c.is_financial else "")
        pos_s = {True: "흑자", False: "적자"}.get(c.op_positive, "-")
        slope_s = f"{c.ma_slope:+.1f}" if c.ma_slope is not None else "-"
        confirm_s = f"{round(c.ma_confirm_ratio * af.MA_CONFIRM_DAYS)}/{af.MA_CONFIRM_DAYS}" \
            if c.ma_confirm_ratio is not None else "-"
        return (
            c.name, c.code, c.sector or "-", mc, f"{c.price:,}", f"{c.pullback:.1f}", f"{c.above_ma:.1f}", slope_s,
            confirm_s, f"{c.trade_value:,.0f}", f"{c.vol_ratio:.1f}", af.fmt_per(c), pbr_s, rel_s, debt_s,
            af.fmt_yoy(c.op_yoy, c.op_turnaround), af.fmt_yoy(c.rev_yoy), pos_s, c.fin_period or "-",
            af.fmt_flow(c.flow_foreign, c.flow_unit), af.fmt_flow(c.flow_orgn, c.flow_unit),
            f"{c.score:.0f}" if c.score is not None else "-", "신규" if c.is_new else "",
        )

    def _fill_tree(self, tree, rows):
        for item in tree.get_children():
            tree.delete(item)
        try:
            limit = max(int(self.var_max_show.get()), 1)
        except (tk.TclError, ValueError):
            limit = af.MAX_SHOW
        for c in rows[:limit]:
            tree.insert("", "end", values=self._cand_values(c))

    def _fill_picks(self, tree, picks, empty_hint):
        for item in tree.get_children():
            tree.delete(item)
        if not picks:
            tree.insert("", "end", values=("-", "기준을 통과한 종목이 없습니다", "", "", "", "", "", "", empty_hint))
            return
        for i, c in enumerate(picks, 1):
            tree.insert("", "end", values=(
                i, c.name, c.code, c.sector or "-", f"{c.price:,}", f"{c.pullback:.1f}", f"{c.above_ma:.1f}",
                f"{c.score:.0f}", " · ".join(c.reasons)))

    # ── 로그 ────────────────────────────────────────────────
    def _set_log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.insert("end", text)
        self.txt_log.configure(state="disabled")

    def _append_log(self, line):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ── CSV ─────────────────────────────────────────────────
    def _export_csv(self):
        res = self._last_result
        if not res or not (res.hits or res.knives or res.picks or res.bounce_picks):
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="auto_finder_result.csv"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["# 스캔 시각", res.as_of, "장중 데이터" if res.intraday else "장마감 기준",
                            "중단됨" if res.stopped else ""])
                w.writerow(["# 조건"] + [f"{k}={v}" for k, v in res.params.items()])
                w.writerow([])
                w.writerow(["구분", "종목명", "코드", "업종", "시총(억)", "현재가", "눌림(%)", "200일선위(%)",
                            "200일선기울기(%)", "최근5일중MA위", "평균거래대금(억)", "거래량비(배)", "PER", "PBR", "업종PER대비(%)",
                            "부채비율(%)", "영업이익YoY(%)", "매출YoY(%)", "영업흑자", "재무기준",
                            "외국인5일", "기관5일", "점수", "변동", "선정이유"])

                def _row(label, c):
                    v = self._cand_values(c)
                    # _cand_values 순서: name, code, sector, mcap, price, pullback, above_ma, slope, tv, vol_ratio,
                    #                   per, pbr, per_rel, debt, op_yoy, rev_yoy, pos, fin_period, flow_f, flow_o, score, new
                    w.writerow([label, *v, " · ".join(c.reasons)])

                for c in res.picks:
                    _row("추천", c)
                for c in res.hits:
                    _row("충족", c)
                for c in res.knives:
                    _row("칼날주의", c)
                for c in res.bounce_picks:
                    _row("단타후보", c)
        except OSError as e:
            messagebox.showerror("저장 실패", f"파일을 저장하지 못했습니다(다른 프로그램에서 열려 있을 수 있어요):\n{e}")
            return
        messagebox.showinfo("저장 완료", f"저장됨: {path}")


if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()
