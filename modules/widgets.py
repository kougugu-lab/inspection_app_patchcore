#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
widgets.py - 共通UIウィジェット
  create_card, Tooltip, HelpWindow, TenKeyDialog
"""

import tkinter as tk
from tkinter import ttk

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE, FONT_HUGE
)


def get_tenkey_keys(is_half_step=False):
    """テンキーの配置を返す。0.5刻みモード時のみ Fr/Rr 切替キーを配置、OFF時は非表示。"""
    if is_half_step:
        return [('7', 0, 0), ('8', 0, 1), ('9', 0, 2),
                ('4', 1, 0), ('5', 1, 1), ('6', 1, 2),
                ('1', 2, 0), ('2', 2, 1), ('3', 2, 2),
                ('0', 3, 0), ('TOGGLE_FR_RR', 3, 1), ('BS', 3, 2)]
    else:
        return [('7', 0, 0), ('8', 0, 1), ('9', 0, 2),
                ('4', 1, 0), ('5', 1, 1), ('6', 1, 2),
                ('1', 2, 0), ('2', 2, 1), ('3', 2, 2),
                ('0', 3, 0), (None, 3, 1), ('BS', 3, 2)]


def get_commit_display_style(is_half_step=False):
    """コミット番号の表示フォントと幅を返す。"""
    if is_half_step:
        # "0001 Fr" / "0001 Rr" 表示用 (操作パネルに収まる 30pt)
        return (FONT_FAMILY, 30, "bold"), 7
    return (FONT_FAMILY, 38, "bold"), 5


def format_commit_for_tenkey(value, is_half_step=False):
    """テンキー入力欄の初期表示用文字列を返す（1.0 → 1 Fr、1.5 → 1 Rr）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not is_half_step:
        return str(int(num))
    is_fr = (num % 1.0 < 0.25)
    tag = "Fr" if is_fr else "Rr"
    return f"{int(num)} {tag}"


def configure_modal_toplevel(win, parent=None):
    """モーダル Toplevel を表示完了後に grab する（Linux/Wayland のクリック不良対策）"""
    if parent is not None:
        try:
            win.transient(parent)
        except tk.TclError:
            pass

    def _drop_topmost():
        if win.winfo_exists():
            try:
                win.attributes("-topmost", False)
            except tk.TclError:
                pass

    def _activate():
        if not win.winfo_exists() or getattr(win, "_modal_grab_active", False):
            return
        win.update_idletasks()
        try:
            if not win.winfo_viewable():
                return
        except tk.TclError:
            return
        try:
            if win.state() == "iconic":
                win.deiconify()
        except tk.TclError:
            pass
        try:
            win.lift()
            win.attributes("-topmost", True)
            win.after(80, _drop_topmost)
        except tk.TclError:
            pass
        try:
            win.focus_force()
        except tk.TclError:
            pass
        try:
            win.grab_set()
            win._modal_grab_active = True
        except tk.TclError:
            pass

    def _on_map(event=None):
        if event is not None and event.widget != win:
            return
        win.after_idle(_activate)

    try:
        win.attributes("-type", "dialog")
    except tk.TclError:
        pass

    win._modal_grab_active = False
    win.bind("<Map>", _on_map, add="+")
    win.after_idle(_on_map)
    # 表示が遅い環境向けフォールバック
    win.after(500, _activate)


def release_modal_toplevel(win):
    """モーダル grab を解放する"""
    try:
        win.grab_release()
    except tk.TclError:
        pass
    win._modal_grab_active = False


def create_card(parent, title=None):
    """共通デザインのカードフレームを作成"""
    frame = tk.Frame(parent, bg=COLOR_BG_PANEL, bd=1, relief="flat")
    inner = tk.Frame(frame, bg=COLOR_BG_PANEL, padx=10, pady=10,
                     highlightbackground=COLOR_BORDER, highlightthickness=1)
    inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    if title:
        lbl = tk.Label(inner, text=title, font=FONT_BOLD,
                       bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, anchor="w")
        lbl.pack(fill=tk.X, pady=(0, 10))
    return frame, inner


class Tooltip:
    """カーソル位置ベースのツールチップ（方向依存なし）"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self._after_id = None
        self.widget.bind("<Enter>", self._schedule)
        self.widget.bind("<Leave>", self.hide_tip)
        self.widget.bind("<Motion>", self._update_pos)

    def _schedule(self, event=None):
        self._last_event = event
        if self._after_id:
            self.widget.after_cancel(self._after_id)
        self._after_id = self.widget.after(500, self._show)

    def _update_pos(self, event=None):
        self._last_event = event
        # ツールチップが既に表示中なら位置を更新
        if self.tip_window:
            self._reposition(event)

    def _show(self):
        if self.tip_window or not self.text:
            return
        ev = getattr(self, '_last_event', None)
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(1)
        label = tk.Label(tw, text=self.text, justify=tk.LEFT,
                         background="#1e2a35", fg="#e0e8f0",
                         relief=tk.SOLID, borderwidth=1,
                         font=(FONT_FAMILY, 10), padx=8, pady=6)
        label.pack(ipadx=1)
        tw.update_idletasks()
        self._reposition(ev)

    def _reposition(self, event=None):
        tw = self.tip_window
        if not tw:
            return
        tw.update_idletasks()
        w_tip = tw.winfo_width()
        h_tip = tw.winfo_height()
        if event:
            cx, cy = event.x_root, event.y_root
        else:
            cx = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
            cy = self.widget.winfo_rooty() + self.widget.winfo_height()
        scr_h = self.widget.winfo_screenheight()
        scr_w = self.widget.winfo_screenwidth()
        x = min(cx + 16, scr_w - w_tip - 4)
        # 下に表示できる場合は下、できない場合は上に表示
        if cy + h_tip + 20 < scr_h:
            y = cy + 16
        else:
            y = cy - h_tip - 10
        tw.wm_geometry(f"+{x}+{y}")

    def hide_tip(self, event=None):
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        tw = self.tip_window
        self.tip_window = None
        if tw:
            tw.destroy()


class HelpWindow(tk.Toplevel):
    def __init__(self, parent, title, help_dict):
        super().__init__(parent)
        self.title(title)
        self.geometry("600x600")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)

        header = tk.Frame(self, bg=COLOR_BG_PANEL, pady=15)
        header.pack(fill=tk.X)
        tk.Label(header, text=title, font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_ACCENT).pack()

        container = tk.Frame(self, bg=COLOR_BG_MAIN, padx=20, pady=20)
        container.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(container, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        scrollable_frame.bind("<Configure>",
                              lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for section, content in help_dict.items():
            tk.Label(scrollable_frame, text=f"■ {section}", font=FONT_BOLD,
                     bg=COLOR_BG_MAIN, fg=COLOR_ACCENT, anchor="w",
                     justify=tk.LEFT).pack(fill=tk.X, pady=(10, 5))
            tk.Label(scrollable_frame, text=content, font=FONT_NORMAL,
                     bg=COLOR_BG_MAIN, fg=COLOR_TEXT_MAIN, anchor="w",
                     justify=tk.LEFT, wraplength=500).pack(fill=tk.X, pady=(0, 15))

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mouse(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mouse(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mouse)
        canvas.bind("<Leave>", _unbind_mouse)

        tk.Button(self, text="閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
                  fg=COLOR_TEXT_MAIN, relief="flat", pady=10,
                  command=self.destroy).pack(fill=tk.X)


class TenKeyDialog(tk.Toplevel):
    def __init__(self, parent, title, initial_value="", is_half_step=False):
        super().__init__(parent)
        self.title(title)
        self.result = None
        self.is_half_step = is_half_step
        self.geometry("420x680")
        self.minsize(420, 580)
        self.resizable(True, True)
        self.configure(bg=COLOR_BG_MAIN)
        configure_modal_toplevel(self, parent)

        self.var_value = tk.StringVar(value=format_commit_for_tenkey(initial_value, is_half_step))
        display_font, display_width = get_commit_display_style(self.is_half_step)

        disp_f = tk.Frame(self, bg=COLOR_BG_MAIN, pady=15)
        disp_f.pack(fill=tk.X)
        tk.Label(disp_f, textvariable=self.var_value, font=display_font,
                 bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat",
                 width=display_width).pack(fill=tk.X, padx=20)

        # 決定/キャンセルを先に BOTTOM へ pack → 常に画面下部に表示される
        btn_f = tk.Frame(self, bg=COLOR_BG_MAIN)
        btn_f.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=12)
        tk.Button(btn_f, text="キャンセル", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", height=2,
                  command=self._close).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=5)
        tk.Button(btn_f, text="決定", font=FONT_BOLD, bg=COLOR_ACCENT,
                  fg="#000000", relief="flat", height=2,
                  command=self.on_enter).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=5)

        # テンキーは残りスペースを埋める
        pad = tk.Frame(self, padx=15, pady=10, bg=COLOR_BG_MAIN)
        pad.pack(fill=tk.BOTH, expand=True)

        self.toggle_btn = None
        keys = get_tenkey_keys(self.is_half_step)
        for (txt, r, c) in keys:
            if txt is None:
                continue
            bg_color = COLOR_BG_PANEL
            if txt == 'BS':
                bg_color = "#D32F2F"

            if txt == 'TOGGLE_FR_RR':
                # 現在が Rr ならボタンは「Fr」、現在が Fr ならボタンは「Rr」を表示
                cur_val = self.var_value.get()
                btn_txt = "Fr" if "Rr" in cur_val else "Rr"
                btn = tk.Button(pad, text=btn_txt, font=FONT_LARGE, bg=COLOR_BG_PANEL,
                                fg=COLOR_ACCENT, activebackground=COLOR_ACCENT,
                                activeforeground=COLOR_BG_MAIN, relief="flat", bd=0,
                                command=lambda: self.on_key('TOGGLE_FR_RR'))
                btn.grid(row=r, column=c, sticky="nsew", padx=4, pady=4)
                self.toggle_btn = btn
            else:
                tk.Button(pad, text=txt, font=FONT_LARGE, bg=bg_color,
                          fg=COLOR_TEXT_MAIN, activebackground=COLOR_ACCENT,
                          activeforeground=COLOR_BG_MAIN, relief="flat", bd=0,
                          command=lambda t=txt: self.on_key(t)).grid(
                    row=r, column=c, sticky="nsew", padx=4, pady=4)

        tk.Button(pad, text="CLR", font=FONT_LARGE, bg="#616161",
                  fg=COLOR_TEXT_MAIN, activebackground=COLOR_ACCENT,
                  activeforeground=COLOR_BG_MAIN, relief="flat", bd=0,
                  command=lambda: self.on_key('CLR')).grid(
            row=4, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)

        for i in range(5):
            pad.rowconfigure(i, weight=1)
        for i in range(3):
            pad.columnconfigure(i, weight=1)

        self._update_toggle_btn()
        self.wait_window(self)

    def _update_toggle_btn(self):
        if self.toggle_btn and self.toggle_btn.winfo_exists():
            cur = self.var_value.get()
            if "Rr" in cur:
                self.toggle_btn.config(text="Fr", fg="#81D4FA")
            else:
                self.toggle_btn.config(text="Rr", fg=COLOR_ACCENT)

    def on_key(self, key):
        cur = self.var_value.get().strip()
        if key == 'CLR':
            self.var_value.set("")
        elif key == 'BS':
            if cur.endswith(" Rr") or cur.endswith(" Fr"):
                self.var_value.set(cur[:-3].strip())
            elif cur.endswith("Rr") or cur.endswith("Fr"):
                self.var_value.set(cur[:-2].strip())
            else:
                self.var_value.set(cur[:-1])
        elif key == 'TOGGLE_FR_RR':
            digits = "".join(c for c in cur if c.isdigit())
            if digits:
                if "Rr" in cur:
                    self.var_value.set(f"{digits} Fr")
                else:
                    self.var_value.set(f"{digits} Rr")
            else:
                if "Rr" in cur:
                    self.var_value.set("Fr")
                else:
                    self.var_value.set("Rr")
        elif len("".join(c for c in cur if c.isdigit())) < 6:
            if self.is_half_step:
                is_rr = ("Rr" in cur)
                tag = "Rr" if is_rr else "Fr"
                digits = "".join(c for c in cur if c.isdigit()) + key
                self.var_value.set(f"{digits} {tag}")
            else:
                self.var_value.set(cur + key)
        self._update_toggle_btn()

    def _close(self):
        release_modal_toplevel(self)
        self.destroy()

    def on_enter(self):
        val = self.var_value.get().strip()
        if val == "":
            self._close()
            return

        try:
            digits = "".join(c for c in val if c.isdigit())
            if not digits:
                self._close()
                return
            base_int = int(digits)
            if "Rr" in val or ".5" in val:
                self.result = float(base_int) + 0.5
            else:
                self.result = float(base_int) if self.is_half_step else base_int
            self._close()
        except ValueError:
            pass
