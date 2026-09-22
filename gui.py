# -*- coding: utf-8 -*-
"""
gui.py — 대장주 자동 발굴기 데스크톱 GUI (Tkinter)

auto_finder.py 의 스캔 로직을 그대로 사용하고, 백그라운드 스레드에서 돌려
진행 상황/로그/결과를 화면에 표시한다.

실행: python gui.py
준비: pip install -r requirements.txt
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import csv

import kis_feed as kf
import auto_finder as af


class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("대장주 자동 발굴기")
        self.geometry("980x680")
        self.minsize(860, 560)

        self._worker = None
        self._stop_requested = False
        self._msg_queue: "queue.Queue" = queue.Queue()

        self._build_widgets()
        self._load_saved_keys()
        self.after(100, self._poll_queue)

    # ── UI 구성 ──────────────────────────────────────────────
    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        # 상단: API 키
        key_frame = ttk.LabelFrame(self, text="KIS API 키")
        key_frame.pack(fill="x", **pad)

        ttk.Label(key_frame, text="APP KEY").grid(row=0, column=0, sticky="e", **pad)
        self.var_app_key = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_key, width=42, show="•").grid(
            row=0, column=1, sticky="w", **pad
        )

        ttk.Label(key_frame, text="APP SECRET").grid(row=0, column=2, sticky="e", **pad)
        self.var_app_secret = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_secret, width=42, show="•").grid(
            row=0, column=3, sticky="w", **pad
        )

        self.var_virtual = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="모의투자 서버 사용", variable=self.var_virtual).grid(
            row=0, column=4, sticky="w", **pad
        )
        ttk.Button(key_frame, text="키 저장", command=self._save_keys).grid(
            row=0, column=5, sticky="w", **pad
        )

        # 조건 설정
        cond_frame = ttk.LabelFrame(self, text="발굴 조건")
        cond_frame.pack(fill="x", **pad)

        self.var_top_n = tk.IntVar(value=af.TOP_N)
        self.var_min_mcap = tk.IntVar(value=af.MIN_MCAP_억)
        self.var_pullback = tk.DoubleVar(value=af.PULLBACK_PCT)
        self.var_lookback = tk.IntVar(value=af.LOOKBACK)
        self.var_ma = tk.IntVar(value=af.MA_PERIOD)
        self.var_max_show = tk.IntVar(value=af.MAX_SHOW)
        self.var_min_trade_value = tk.DoubleVar(value=af.MIN_AVG_TRADE_VALUE_억)
        self.var_max_per = tk.DoubleVar(value=af.MAX_PER)
        self.var_max_pbr = tk.DoubleVar(value=af.MAX_PBR)

        fields_row1 = [
            ("시총 상위 N개", self.var_top_n, 8),
            ("최소 시총(억)", self.var_min_mcap, 10),
            ("눌림 기준(%)", self.var_pullback, 8),
            ("고점 산정일", self.var_lookback, 8),
            ("이동평균일", self.var_ma, 8),
            ("결과 최대표시", self.var_max_show, 8),
        ]
        for i, (label, var, width) in enumerate(fields_row1):
            ttk.Label(cond_frame, text=label).grid(row=0, column=i * 2, sticky="e", **pad)
            ttk.Entry(cond_frame, textvariable=var, width=width).grid(
                row=0, column=i * 2 + 1, sticky="w", **pad
            )

        fields_row2 = [
            ("최소 평균거래대금(억)", self.var_min_trade_value, 8),
            ("PER 상한(0=미사용)", self.var_max_per, 8),
            ("PBR 상한(0=미사용)", self.var_max_pbr, 8),
        ]
        for i, (label, var, width) in enumerate(fields_row2):
            ttk.Label(cond_frame, text=label).grid(row=1, column=i * 2, sticky="e", **pad)
            ttk.Entry(cond_frame, textvariable=var, width=width).grid(
                row=1, column=i * 2 + 1, sticky="w", **pad
            )

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

        # 결과: 탭이 아니라 위/아래로 같이 보이게 표시
        result_frame = ttk.Frame(self)
        result_frame.pack(fill="both", expand=True, **pad)
        result_frame.rowconfigure(0, weight=3)
        result_frame.rowconfigure(1, weight=2)
        result_frame.columnconfigure(0, weight=1)

        hits_box = ttk.LabelFrame(result_frame, text="🟢 조건 충족 — 대형주 + 상승추세 + 눌림")
        hits_box.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
        self.tree_hits = self._make_tree(hits_box, height=10)

        knives_box = ttk.LabelFrame(result_frame, text="⚠️ 칼날 주의 — 눌림은 왔지만 하락추세(200일선 아래)")
        knives_box.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.tree_knives = self._make_tree(knives_box, height=7)

        # 로그
        log_frame = ttk.LabelFrame(self, text="로그")
        log_frame.pack(fill="both", **pad)
        self.txt_log = tk.Text(log_frame, height=8, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

    def _make_tree(self, parent, height=10):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        cols = ("name", "code", "mcap", "price", "pullback", "above_ma", "trade_value", "vol_ratio", "per", "pbr")
        headers = {
            "name": "종목명", "code": "코드", "mcap": "시총(억)",
            "price": "현재가", "pullback": "눌림(%)", "above_ma": "200일선위(%)",
            "trade_value": "평균거래대금(억)", "vol_ratio": "거래량비(배)",
            "per": "PER", "pbr": "PBR",
        }
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=height)
        for c in cols:
            tree.heading(c, text=headers[c])
            tree.column(c, width=120, anchor="center")
        tree.column("name", width=160, anchor="w")
        tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)
        return tree

    # ── 키 저장/로딩 ─────────────────────────────────────────
    def _load_saved_keys(self):
        cfg = kf._read_key_file()
        if cfg:
            self.var_app_key.set(cfg.get("app_key", ""))
            self.var_app_secret.set(cfg.get("app_secret", ""))
            self.var_virtual.set(bool(cfg.get("is_virtual", False)))

    def _save_keys(self):
        app_key = self.var_app_key.get().strip()
        app_secret = self.var_app_secret.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("입력 필요", "APP KEY / APP SECRET 을 입력하세요.")
            return
        kf.set_keys(app_key, app_secret, self.var_virtual.get(), save=True)
        messagebox.showinfo("저장 완료", "kis_keys.json 에 저장했습니다.")

    # ── 스캔 실행 ────────────────────────────────────────────
    def _start_scan(self):
        if self._worker and self._worker.is_alive():
            return

        app_key = self.var_app_key.get().strip()
        app_secret = self.var_app_secret.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("입력 필요", "APP KEY / APP SECRET 을 입력하세요.")
            return
        kf.set_keys(app_key, app_secret, self.var_virtual.get())

        for tree in (self.tree_hits, self.tree_knives):
            for item in tree.get_children():
                tree.delete(item)
        self._set_log("")
        self.progress["value"] = 0
        self.btn_export.configure(state="disabled")

        self._stop_requested = False
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.lbl_progress.configure(text="시작 중...")

        params = dict(
            top_n=self.var_top_n.get(),
            min_mcap_억=self.var_min_mcap.get(),
            pullback_pct=self.var_pullback.get(),
            lookback=self.var_lookback.get(),
            ma_period=self.var_ma.get(),
            min_avg_trade_value_억=self.var_min_trade_value.get(),
            max_per=self.var_max_per.get(),
            max_pbr=self.var_max_pbr.get(),
        )

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
            hits, knives, scanned, uni_len = af.run_scan(
                progress_cb=progress_cb, log_cb=log_cb, stop_flag=stop_flag, **params
            )
            self._msg_queue.put(("done", hits, knives, scanned, uni_len))
        except ImportError:
            self._msg_queue.put(("error", "FinanceDataReader 가 설치되어 있지 않습니다.\npip install finance-datareader"))
        except Exception as e:
            self._msg_queue.put(("error", f"스캔 실패: {e}"))

    # ── 메인스레드 큐 폴링 ───────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                item = self._msg_queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, done, total, code, name = item
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.lbl_progress.configure(text=f"{done}/{total}  {name}({code})")
                elif kind == "log":
                    self._append_log(item[1])
                elif kind == "done":
                    _, hits, knives, scanned, uni_len = item
                    self._fill_tree(self.tree_hits, hits)
                    self._fill_tree(self.tree_knives, knives)
                    self._last_hits, self._last_knives = hits, knives
                    self.btn_export.configure(state="normal" if (hits or knives) else "disabled")
                    self.lbl_progress.configure(text=f"완료 — 조회 {scanned}/{uni_len}, 충족 {len(hits)}, 칼날 {len(knives)}")
                    self._finish_run()
                elif kind == "error":
                    self._append_log(f"[오류] {item[1]}")
                    messagebox.showerror("오류", item[1])
                    self._finish_run()
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish_run(self):
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _fill_tree(self, tree, rows):
        for item in tree.get_children():
            tree.delete(item)
        for name, code, mcap, price, pb, above, tv, vr, per, pbr in rows[: self.var_max_show.get()]:
            mc = f"{mcap:,}" if mcap else "-"
            per_s = f"{per:.1f}" if per is not None else "-"
            pbr_s = f"{pbr:.2f}" if pbr is not None else "-"
            tree.insert(
                "", "end",
                values=(name, code, mc, f"{price:,}", f"{pb:.1f}", f"{above:.1f}", f"{tv:,.0f}", f"{vr:.1f}", per_s, pbr_s),
            )

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

    def _export_csv(self):
        hits = getattr(self, "_last_hits", [])
        knives = getattr(self, "_last_knives", [])
        if not hits and not knives:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="auto_finder_result.csv"
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["구분", "종목명", "코드", "시총(억)", "현재가", "눌림(%)", "200일선위(%)", "평균거래대금(억)", "거래량비(배)", "PER", "PBR"])
            for name, code, mcap, price, pb, above, tv, vr, per, pbr in hits:
                w.writerow(["충족", name, code, mcap, price, f"{pb:.2f}", f"{above:.2f}", f"{tv:.1f}", f"{vr:.2f}",
                            (f"{per:.2f}" if per is not None else ""), (f"{pbr:.2f}" if pbr is not None else "")])
            for name, code, mcap, price, pb, above, tv, vr, per, pbr in knives:
                w.writerow(["칼날주의", name, code, mcap, price, f"{pb:.2f}", f"{above:.2f}", f"{tv:.1f}", f"{vr:.2f}",
                            (f"{per:.2f}" if per is not None else ""), (f"{pbr:.2f}" if pbr is not None else "")])
        messagebox.showinfo("저장 완료", f"저장됨: {path}")


if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()
