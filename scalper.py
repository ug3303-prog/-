# -*- coding: utf-8 -*-
"""
scalper.py — 초단타(몇 분~몇 시간) 모니터링 전용 툴.

⚠️ 이 프로그램은 신호를 계산해서 보여줄 뿐, 매수/매도 주문을 절대 넣지 않는다.
   실제 매매는 증권사 앱/HTS에서 본인이 직접 판단해서 누른다.

워치리스트에 넣은 종목을 주기적으로 폴링해서:
  ① 호가 매수/매도 잔량 비율 (매수우위/매도우위)
  ② 체결강도 (매수체결량/매도체결량*100 — 100 넘으면 매수가 더 세게 붙는 중)
  ③ 폴링 주기 동안의 거래량 급증
  ④ 직전 폴링 대비 급등/급락
을 계산해서 표에 보여준다. 단일 신호 1개만 뜬 건 노이즈일 확률이 높아서
로그엔 쿨다운을 걸어 자주 안 남기고, **신호 2개 이상이 같은 방향으로 동시에
뜨는 "복합 신호"만 🔥로 바로 로그**에 남긴다.

복합 신호가 뜨면 1분/3분/5분 뒤 실제로 그 방향으로 움직였는지 자동으로
추적해서 "신호 검증" 로그에 남기고 적중률을 집계한다 — 이 신호들을 믿어도
되는지 스스로 판단할 근거를 쌓기 위함이다. 기록은 signal_log.csv에 누적된다.

보유 포지션 추적도 있다: 매수가/수량/손절%/목표% 를 등록해두면 현재가가
손절가·목표가에 닿았을 때 알림을 준다(주문은 안 넣음 — 알림만).

포지션 크기 계산기도 포함: 투자금 + 손절% 를 넣으면 몇 주를 살 수 있는지,
목표가/손절가가 얼마인지 계산해준다 — 이것도 참고용 숫자일 뿐 자동 주문 아님.

실행: python scalper.py
준비: pip install -r requirements.txt   (kis_feed.py 도 같은 폴더에)
"""
from __future__ import annotations

import csv
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import kis_feed as kf

# ── 신호 기준값 (GUI에서도 조절 가능) ───────────────────────────
DEFAULT_POLL_SEC = 3          # 폴링 주기(초). 종목 수 * 2회 호출 / 주기 가 KIS 레이트리밋 이내여야 함
BID_ASK_RATIO_HIGH = 1.8      # 총매수잔량/총매도잔량 이 이 값 넘으면 "매수우위"
BID_ASK_RATIO_LOW = 1 / 1.8   # 이 값 밑돌면 "매도우위"
CTTR_HIGH = 150.0             # 체결강도 100 = 매수/매도 균형. 150 이상이면 매수 쏠림
CTTR_LOW = 70.0                # 70 이하면 매도 쏠림
SURGE_PCT = 1.0                # 폴링 간격 사이 가격이 이 비율(%) 이상 움직이면 급등/급락 알림
VOLUME_SURGE_RATIO = 3.0       # 직전 폴링 대비 거래량 증가분이 이 배수 넘으면 거래량 급증 알림

SINGLE_SIGNAL_COOLDOWN_SEC = 45   # 단일 신호(노이즈 가능성 높음)는 종목당 이 시간 안엔 중복 로그 안 함
VERIFY_HORIZONS_SEC = (60, 180, 300)   # 복합신호 적중 검증 시점: 1분/3분/5분 후
VERIFY_STAT_HORIZON_SEC = 180          # 적중률 집계는 이 시점(3분) 기준으로만
VERIFY_HIT_THRESHOLD_PCT = 0.1         # 기대 방향으로 이 이상 움직였으면 '적중'
SIGNAL_LOG_FILE = Path(__file__).with_name("signal_log.csv")


class Watcher:
    """종목 1개의 최근 상태를 들고 있다가, 새 데이터가 오면 신호를 계산한다."""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name or code
        self.last_price = None
        self.last_volume = None
        self.last_update = None
        self._prev_delta = None

    def update(self, rt: dict, ob: dict | None):
        """
        반환: (labels: list[str], composite: bool, direction: "up"|"down"|None)
        composite = 신호 2개 이상이 서로 충돌 없이(같은 방향으로) 동시에 떴을 때 True.
        direction 은 composite일 때만 의미 있음 — 검증 추적에 쓴다.
        """
        items = []  # (label, direction)
        price = rt.get("price")
        volume = rt.get("volume")

        if self.last_price is not None and price:
            move_pct = (price - self.last_price) / self.last_price * 100
            if move_pct >= SURGE_PCT:
                items.append((f"급등 +{move_pct:.1f}%", "up"))
            elif move_pct <= -SURGE_PCT:
                items.append((f"급락 {move_pct:.1f}%", "down"))

        if self.last_volume is not None and volume is not None:
            delta = volume - self.last_volume
            if self._prev_delta and delta > self._prev_delta * VOLUME_SURGE_RATIO:
                items.append((f"거래량 급증({delta:,}주)", None))
            self._prev_delta = delta

        cttr = rt.get("cttr")
        if cttr is not None:
            if cttr >= CTTR_HIGH:
                items.append((f"체결강도 매수쏠림({cttr:.0f})", "up"))
            elif cttr <= CTTR_LOW:
                items.append((f"체결강도 매도쏠림({cttr:.0f})", "down"))

        if ob and ob.get("bid_ask_ratio") is not None:
            r = ob["bid_ask_ratio"]
            if r >= BID_ASK_RATIO_HIGH:
                items.append((f"호가 매수우위({r:.1f}배)", "up"))
            elif r <= BID_ASK_RATIO_LOW:
                items.append((f"호가 매도우위({1/r if r else 0:.1f}배)", "down"))

        self.last_price = price
        self.last_volume = volume
        self.last_update = time.time()

        labels = [l for l, _ in items]
        dirs = {d for _, d in items if d}
        composite = len(items) >= 2 and len(dirs) <= 1
        direction = next(iter(dirs)) if (composite and dirs) else None
        return labels, composite, direction


class ScalperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("초단타 모니터링 (신호 전용 — 자동매매 아님)")
        self.geometry("980x640")
        self.minsize(820, 520)

        self._watchers: dict[str, Watcher] = {}
        self._poll_thread = None
        self._stop_requested = False
        self._msg_queue: "queue.Queue" = queue.Queue()

        self._last_single_log: dict[str, float] = {}   # code -> 마지막 단일신호 로그 시각(쿨다운용)
        self._last_price: dict[str, float] = {}         # code -> 가장 최근 수신 현재가(검증용)
        self._pending_verifications: list[dict] = []    # 복합신호 적중 검증 대기열
        self._verify_stats = {"total": 0, "hit": 0}

        self._positions: dict[str, dict] = {}            # code -> {entry, qty, stop, target, stop_alerted, target_alerted}

        self._build_widgets()
        self._load_saved_keys()
        self.after(100, self._poll_queue)

    # ── UI 구성 ──────────────────────────────────────────────
    def _build_widgets(self):
        pad = {"padx": 6, "pady": 4}

        warn = ttk.Label(
            self,
            text="⚠️ 신호만 보여줍니다. 매수/매도는 직접 증권사 앱에서 누르세요. 공부용 소액으로만 운영하세요.",
            foreground="#b00020",
        )
        warn.pack(fill="x", padx=6, pady=(6, 0))

        key_frame = ttk.LabelFrame(self, text="KIS API 키")
        key_frame.pack(fill="x", **pad)
        ttk.Label(key_frame, text="APP KEY").grid(row=0, column=0, sticky="e", **pad)
        self.var_app_key = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_key, width=40, show="•").grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(key_frame, text="APP SECRET").grid(row=0, column=2, sticky="e", **pad)
        self.var_app_secret = tk.StringVar()
        ttk.Entry(key_frame, textvariable=self.var_app_secret, width=40, show="•").grid(
            row=0, column=3, sticky="w", **pad
        )
        self.var_virtual = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="모의투자", variable=self.var_virtual).grid(
            row=0, column=4, sticky="w", **pad
        )
        ttk.Button(key_frame, text="키 저장", command=self._save_keys).grid(
            row=0, column=5, sticky="w", **pad
        )

        wl_frame = ttk.LabelFrame(self, text="워치리스트")
        wl_frame.pack(fill="x", **pad)
        ttk.Label(wl_frame, text="종목코드(쉼표로 여러 개, 예: 005930,000660)").grid(
            row=0, column=0, sticky="e", **pad
        )
        self.var_codes = tk.StringVar()
        ttk.Entry(wl_frame, textvariable=self.var_codes, width=40).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(wl_frame, text="폴링주기(초)").grid(row=0, column=2, sticky="e", **pad)
        self.var_poll_sec = tk.IntVar(value=DEFAULT_POLL_SEC)
        ttk.Entry(wl_frame, textvariable=self.var_poll_sec, width=6).grid(
            row=0, column=3, sticky="w", **pad
        )
        self.btn_run = ttk.Button(wl_frame, text="▶ 모니터링 시작", command=self._start)
        self.btn_run.grid(row=0, column=4, sticky="w", **pad)
        self.btn_stop = ttk.Button(wl_frame, text="■ 중단", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=5, sticky="w", **pad)

        table_frame = ttk.LabelFrame(self, text="실시간 현황")
        table_frame.pack(fill="both", expand=True, **pad)
        cols = ("name", "code", "price", "change_pct", "cttr", "bid_ask_ratio", "signals")
        headers = {
            "name": "종목명", "code": "코드", "price": "현재가", "change_pct": "등락률(%)",
            "cttr": "체결강도", "bid_ask_ratio": "호가매수/매도비", "signals": "신호",
        }
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=14)
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=110, anchor="center")
        self.tree.column("name", width=120, anchor="w")
        self.tree.column("signals", width=260, anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.tag_configure("up", foreground="#c0392b")
        self.tree.tag_configure("down", foreground="#2980b9")

        calc_frame = ttk.LabelFrame(self, text="포지션 크기 계산기 (참고용 — 자동 주문 아님)")
        calc_frame.pack(fill="x", **pad)
        self.var_capital = tk.IntVar(value=1_000_000)
        self.var_entry_price = tk.IntVar(value=0)
        self.var_stop_pct = tk.DoubleVar(value=2.0)
        self.var_target_pct = tk.DoubleVar(value=4.0)
        ttk.Label(calc_frame, text="투자금(원)").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(calc_frame, textvariable=self.var_capital, width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(calc_frame, text="매수가(원)").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(calc_frame, textvariable=self.var_entry_price, width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(calc_frame, text="손절%").grid(row=0, column=4, sticky="e", **pad)
        ttk.Entry(calc_frame, textvariable=self.var_stop_pct, width=6).grid(row=0, column=5, sticky="w", **pad)
        ttk.Label(calc_frame, text="목표%").grid(row=0, column=6, sticky="e", **pad)
        ttk.Entry(calc_frame, textvariable=self.var_target_pct, width=6).grid(row=0, column=7, sticky="w", **pad)
        ttk.Button(calc_frame, text="계산", command=self._calc_position).grid(row=0, column=8, sticky="w", **pad)
        ttk.Button(calc_frame, text="→ 포지션 등록", command=self._register_position_from_calc).grid(
            row=0, column=9, sticky="w", **pad
        )
        self.lbl_calc_result = ttk.Label(calc_frame, text="")
        self.lbl_calc_result.grid(row=1, column=0, columnspan=10, sticky="w", **pad)

        pos_frame = ttk.LabelFrame(self, text="보유 포지션 추적 (손절가/목표가 도달 알림 — 주문은 안 넣음)")
        pos_frame.pack(fill="both", **pad)
        pos_cols = ("name", "code", "entry", "qty", "stop", "target", "price", "pnl", "status")
        pos_headers = {
            "name": "종목명", "code": "코드", "entry": "매수가", "qty": "수량",
            "stop": "손절가", "target": "목표가", "price": "현재가", "pnl": "손익(%)", "status": "상태",
        }
        self.pos_tree = ttk.Treeview(pos_frame, columns=pos_cols, show="headings", height=4)
        for c in pos_cols:
            self.pos_tree.heading(c, text=pos_headers[c])
            self.pos_tree.column(c, width=90, anchor="center")
        self.pos_tree.column("name", width=110, anchor="w")
        self.pos_tree.pack(fill="both", expand=True, side="left", padx=4, pady=4)
        self.pos_tree.tag_configure("stop_hit", foreground="#2980b9")
        self.pos_tree.tag_configure("target_hit", foreground="#c0392b")
        ttk.Button(pos_frame, text="선택 포지션 삭제", command=self._remove_selected_position).pack(
            side="right", padx=6, pady=4, anchor="n"
        )

        log_frame = ttk.LabelFrame(self, text="신호 알림 로그 / 적중 검증")
        log_frame.pack(fill="both", **pad)
        self.lbl_stats = ttk.Label(log_frame, text="복합신호 적중률(3분 기준): 아직 데이터 없음")
        self.lbl_stats.pack(fill="x", padx=4, pady=(4, 0))
        self.txt_log = tk.Text(log_frame, height=7, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=4, pady=4)

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

    # ── 포지션 계산기 ────────────────────────────────────────
    def _calc_position(self):
        result = self._compute_position()
        if not result:
            self.lbl_calc_result.configure(text="매수가를 입력하세요.")
            return
        qty, stop_price, target_price, max_loss, max_gain = result
        self.lbl_calc_result.configure(
            text=(
                f"매수가능 {qty:,}주(실제 체결은 호가 단위로 조정) · "
                f"손절가 {stop_price:,}원(최대손실 ≈{max_loss:,}원) · "
                f"목표가 {target_price:,}원(기대이익 ≈{max_gain:,}원)"
            )
        )

    def _compute_position(self):
        capital = self.var_capital.get()
        entry = self.var_entry_price.get()
        stop_pct = self.var_stop_pct.get()
        target_pct = self.var_target_pct.get()
        if entry <= 0:
            return None
        qty = capital // entry
        stop_price = round(entry * (1 - stop_pct / 100))
        target_price = round(entry * (1 + target_pct / 100))
        max_loss = qty * (entry - stop_price)
        max_gain = qty * (target_price - entry)
        return qty, stop_price, target_price, max_loss, max_gain

    # ── 포지션 추적 ──────────────────────────────────────────
    def _register_position_from_calc(self):
        code = None
        codes = [c.strip().zfill(6) for c in self.var_codes.get().split(",") if c.strip()]
        if len(codes) == 1:
            code = codes[0]
        if not code:
            messagebox.showwarning(
                "종목 특정 필요",
                "워치리스트에 종목을 정확히 1개만 입력한 상태에서 눌러주세요.\n"
                "(여러 종목이면 어떤 종목 포지션인지 특정할 수 없습니다)",
            )
            return
        result = self._compute_position()
        if not result:
            messagebox.showwarning("입력 필요", "매수가를 입력하세요.")
            return
        qty, stop_price, target_price, _, _ = result
        entry = self.var_entry_price.get()
        self._positions[code] = {
            "entry": entry, "qty": qty, "stop": stop_price, "target": target_price,
            "stop_alerted": False, "target_alerted": False,
        }
        if code not in self._watchers:
            self._watchers[code] = Watcher(code)
        self._refresh_position_row(code)
        self._append_log(f"[포지션 등록] {code} 매수가 {entry:,} · 손절 {stop_price:,} · 목표 {target_price:,}")

    def _remove_selected_position(self):
        sel = self.pos_tree.selection()
        for iid in sel:
            self._positions.pop(iid, None)
            self.pos_tree.delete(iid)

    def _refresh_position_row(self, code):
        pos = self._positions.get(code)
        if not pos:
            return
        watcher = self._watchers.get(code)
        name = watcher.name if watcher else code
        price = self._last_price.get(code)
        pnl = ((price - pos["entry"]) / pos["entry"] * 100) if price else None
        tag = ""
        status = "보유중"
        if price is not None:
            if price <= pos["stop"]:
                status = "⚠ 손절가 도달"
                tag = "stop_hit"
            elif price >= pos["target"]:
                status = "🎯 목표가 도달"
                tag = "target_hit"

        values = (
            name, code, f"{pos['entry']:,}", f"{pos['qty']:,}",
            f"{pos['stop']:,}", f"{pos['target']:,}",
            f"{price:,}" if price is not None else "-",
            f"{pnl:+.2f}" if pnl is not None else "-",
            status,
        )
        if self.pos_tree.exists(code):
            self.pos_tree.item(code, values=values, tags=(tag,) if tag else ())
        else:
            self.pos_tree.insert("", "end", iid=code, values=values, tags=(tag,) if tag else ())

    def _check_position_alerts(self, code, price):
        pos = self._positions.get(code)
        if not pos or price is None:
            return
        watcher = self._watchers.get(code)
        name = watcher.name if watcher else code
        if price <= pos["stop"] and not pos["stop_alerted"]:
            pos["stop_alerted"] = True
            self._append_log(f"⚠️ [{name}] 손절가({pos['stop']:,}) 도달 — 손절 고려 (직접 판단)")
        if price >= pos["target"] and not pos["target_alerted"]:
            pos["target_alerted"] = True
            self._append_log(f"🎯 [{name}] 목표가({pos['target']:,}) 도달 — 익절 고려 (직접 판단)")

    # ── 모니터링 시작/중단 ───────────────────────────────────
    def _start(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return

        app_key = self.var_app_key.get().strip()
        app_secret = self.var_app_secret.get().strip()
        if not app_key or not app_secret:
            messagebox.showwarning("입력 필요", "APP KEY / APP SECRET 을 입력하세요.")
            return
        kf.set_keys(app_key, app_secret, self.var_virtual.get())

        wl_codes = [c.strip().zfill(6) for c in self.var_codes.get().split(",") if c.strip()]
        # 워치리스트 + 이미 등록된 포지션 종목을 합쳐서 감시 대상으로 삼는다
        codes = list(dict.fromkeys(wl_codes + list(self._positions.keys())))
        if not codes:
            messagebox.showwarning("입력 필요", "종목코드를 1개 이상 입력하세요.")
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._watchers = {c: Watcher(c) for c in codes}
        for c in codes:
            self.tree.insert("", "end", iid=c, values=(c, c, "-", "-", "-", "-", ""))
        for c in self._positions:
            self._refresh_position_row(c)

        self._stop_requested = False
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        poll_sec = max(1, self.var_poll_sec.get())
        # self._watchers.keys()를 매 루프 다시 읽기 때문에, 모니터링 도중
        # "→ 포지션 등록"으로 추가된 새 종목도 다음 폴링부터 자동으로 감시 대상에 들어간다.
        self._poll_thread = threading.Thread(target=self._poll_worker, args=(poll_sec,), daemon=True)
        self._poll_thread.start()

    def _stop(self):
        self._stop_requested = True
        self.btn_stop.configure(state="disabled")

    def _poll_worker(self, poll_sec):
        try:
            kf._load_keys()
        except Exception as e:
            self._msg_queue.put(("error", str(e)))
            return

        while not self._stop_requested:
            t0 = time.time()
            codes = list(self._watchers.keys())  # 도중에 포지션 등록으로 늘어날 수 있어 매번 새로 읽음
            for code in codes:
                if self._stop_requested:
                    break
                watcher = self._watchers.get(code)
                if watcher is None:
                    continue
                try:
                    rt = kf.fetch_realtime(code)
                    ob = kf.fetch_orderbook(code)
                except Exception as e:
                    self._msg_queue.put(("row_error", code, str(e)))
                    continue
                if not rt:
                    continue
                labels, composite, direction = watcher.update(rt, ob)
                price = rt.get("price")
                self._msg_queue.put(("row", code, rt, ob, labels))

                if composite:
                    tag = "🔥"
                    self._msg_queue.put((
                        "log", f"{tag} [{watcher.name or code}] 복합신호: " + " / ".join(labels)
                    ))
                    if direction and price:
                        self._msg_queue.put(("verify_new", code, watcher.name, price, direction, time.time()))
                elif labels:
                    now = time.time()
                    last = self._last_single_log.get(code, 0)
                    if now - last >= SINGLE_SIGNAL_COOLDOWN_SEC:
                        self._last_single_log[code] = now
                        self._msg_queue.put(("log", f"[{watcher.name or code}] " + " / ".join(labels)))

            elapsed = time.time() - t0
            wait = max(0.0, poll_sec - elapsed)
            end_at = time.time() + wait
            while not self._stop_requested and time.time() < end_at:
                time.sleep(0.1)

        self._msg_queue.put(("stopped", None))

    # ── 메인스레드 큐 폴링 ───────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                item = self._msg_queue.get_nowait()
                kind = item[0]
                if kind == "row":
                    _, code, rt, ob, labels = item
                    self._update_row(code, rt, ob, labels)
                elif kind == "log":
                    self._append_log(item[1])
                elif kind == "row_error":
                    self._append_log(f"[경고] {item[1]} 조회 실패: {item[2]}")
                elif kind == "verify_new":
                    _, code, name, price, direction, t = item
                    self._pending_verifications.append({
                        "code": code, "name": name or code, "price": price,
                        "direction": direction, "time": t, "done": set(),
                    })
                elif kind == "error":
                    messagebox.showerror("오류", item[1])
                    self._finish()
                elif kind == "stopped":
                    self._finish()
        except queue.Empty:
            pass
        self._check_verifications()
        self.after(200, self._poll_queue)

    def _finish(self):
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    # ── 신호 적중 검증 ───────────────────────────────────────
    def _check_verifications(self):
        now = time.time()
        still_pending = []
        for v in self._pending_verifications:
            cur_price = self._last_price.get(v["code"])
            elapsed = now - v["time"]
            for horizon in VERIFY_HORIZONS_SEC:
                if horizon in v["done"] or elapsed < horizon or cur_price is None:
                    continue
                v["done"].add(horizon)
                change_pct = (cur_price - v["price"]) / v["price"] * 100
                if v["direction"] == "up":
                    hit = change_pct >= VERIFY_HIT_THRESHOLD_PCT
                else:
                    hit = change_pct <= -VERIFY_HIT_THRESHOLD_PCT
                mark = "✅적중" if hit else "❌불발"
                mins = horizon // 60
                self._append_log(
                    f"[검증 {mins}분] {v['name']} {('매수' if v['direction']=='up' else '매도')}신호 → "
                    f"{change_pct:+.2f}% {mark}"
                )
                self._log_verification_csv(v, horizon, cur_price, change_pct, hit)
                if horizon == VERIFY_STAT_HORIZON_SEC:
                    self._verify_stats["total"] += 1
                    if hit:
                        self._verify_stats["hit"] += 1
                    self._update_stats_label()
            if len(v["done"]) < len(VERIFY_HORIZONS_SEC):
                still_pending.append(v)
        self._pending_verifications = still_pending

    def _update_stats_label(self):
        total = self._verify_stats["total"]
        hit = self._verify_stats["hit"]
        if total == 0:
            self.lbl_stats.configure(text="복합신호 적중률(3분 기준): 아직 데이터 없음")
        else:
            self.lbl_stats.configure(
                text=f"복합신호 적중률(3분 기준): {hit}/{total} ({hit/total*100:.0f}%)"
            )

    def _log_verification_csv(self, v, horizon, cur_price, change_pct, hit):
        try:
            is_new = not SIGNAL_LOG_FILE.exists()
            with open(SIGNAL_LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(["시각", "종목명", "코드", "방향", "신호가", "검증시점(초)", "검증가", "변동%", "적중"])
                w.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v["time"])),
                    v["name"], v["code"], v["direction"], v["price"], horizon, cur_price,
                    f"{change_pct:.2f}", "Y" if hit else "N",
                ])
        except Exception:
            pass  # 로그 파일 기록 실패는 모니터링 자체를 막을 이유가 아니다

    def _update_row(self, code, rt, ob, signals):
        watcher = self._watchers.get(code)
        name = watcher.name if watcher else code
        price = rt.get("price")
        change_pct = rt.get("change_pct")
        cttr = rt.get("cttr")
        ratio = ob.get("bid_ask_ratio") if ob else None

        if price is not None:
            self._last_price[code] = price

        tag = ""
        if change_pct is not None:
            tag = "up" if change_pct > 0 else ("down" if change_pct < 0 else "")

        values = (
            name, code,
            f"{price:,}" if price is not None else "-",
            f"{change_pct:+.2f}" if change_pct is not None else "-",
            f"{cttr:.0f}" if cttr is not None else "-",
            f"{ratio:.2f}" if ratio is not None else "-",
            " / ".join(signals),
        )
        if self.tree.exists(code):
            self.tree.item(code, values=values, tags=(tag,) if tag else ())
        else:
            self.tree.insert("", "end", iid=code, values=values, tags=(tag,) if tag else ())

        if code in self._positions:
            self._check_position_alerts(code, price)
            self._refresh_position_row(code)

    def _append_log(self, line):
        ts = time.strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{ts}] {line}\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")


if __name__ == "__main__":
    app = ScalperApp()
    app.mainloop()
