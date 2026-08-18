#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dialogs.py - ダイアログウィンドウ (PatchCore対応版)
"""

import json
import os
import time
import copy
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import numpy as np
from pathlib import Path

import cv2
from PIL import Image, ImageTk

if __name__ == "__main__" or __package__ is None:
    import sys
    _here = os.path.dirname(os.path.abspath(__file__))
    _code_dir = os.path.dirname(_here)
    if _code_dir not in sys.path:
        sys.path.insert(0, _code_dir)

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_OK, COLOR_NG, COLOR_NG_MUTED, COLOR_WARNING,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_LARGE,
    FONT_SET_TAB, FONT_SET_LBL, FONT_SET_VAL, FONT_BTN_LARGE,
    RES_OPTIONS, RES_OPTIONS_PREVIEW, RES_OPTIONS_SAVE,
    VALID_BCM_PINS
)
from .hardware import DigitalInputDevice, OutputDevice
from .widgets import create_card, Tooltip, HelpWindow, configure_modal_toplevel, release_modal_toplevel

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


def detect_available_cameras():
    """OSが認識しているカメラデバイスを高速探索し、
    [(index_int, display_label_str), ...] のリストを返す
    """
    import sys
    import subprocess
    import cv2
    import os

    # OpenCV のキャプチャ失敗警告ログを消音
    try:
        if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass

    devices = []

    if sys.platform.startswith("linux"):
        # Linux (Raspberry Pi 等): /sys/class/video4linux/video*/name を軽量チェック
        v4l_dir = "/sys/class/video4linux"
        if os.path.exists(v4l_dir):
            ignore_keywords = [
                "codec", "rpivid", "vc4", "media-controller", "bcm2835-isp",
                "bcm2835-codec", "h264", "hevc", "vp8", "isp", "decoder", "encoder", "unicam-isp"
            ]
            for entry in sorted(os.listdir(v4l_dir), key=lambda x: int(x.replace("video", "")) if x.replace("video", "").isdigit() else 999):
                if entry.startswith("video"):
                    try:
                        idx = int(entry.replace("video", ""))
                        name_file = os.path.join(v4l_dir, entry, "name")
                        cam_name = f"カメラ {idx}"
                        if os.path.exists(name_file):
                            with open(name_file, "r", encoding="utf-8", errors="ignore") as f:
                                name_text = f.read().strip()
                                if name_text:
                                    cam_name = name_text
                        
                        # 非カメラ（bcm2835-codec, bcm2835-isp などのデコーダ/エンコーダ/ISPノード）を即座に除外
                        if any(k in cam_name.lower() for k in ignore_keywords):
                            continue

                        # 実際のカメラノードのみ簡易キャプチャ確認 (CAP_V4L2)
                        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
                        if cap and cap.isOpened():
                            ret, _ = cap.read()
                            if ret:
                                devices.append((idx, f"[{idx}] {cam_name}"))
                            cap.release()
                    except Exception:
                        pass
    elif sys.platform.startswith("win"):
        # Windows: PowerShell で PnP カメラデバイス名を取得
        names_from_ps = []
        try:
            ps_cmd = 'Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPClass -eq "Camera" -or $_.PNPClass -eq "Image"} | Select-Object -ExpandProperty Name'
            res = subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout:
                names_from_ps = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception:
            pass

        open_indices = []
        for idx in range(6):  # 高速化のため最大 6 デバイス探索
            try:
                cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                if not cap.isOpened():
                    cap = cv2.VideoCapture(idx, cv2.CAP_ANY)
                if cap and cap.isOpened():
                    ret, _ = cap.read()
                    if ret:
                        open_indices.append(idx)
                    cap.release()
            except Exception:
                pass

        for i, idx in enumerate(open_indices):
            if i < len(names_from_ps):
                d_name = names_from_ps[i]
            else:
                d_name = f"USB Camera {idx}"
            devices.append((idx, f"[{idx}] {d_name}"))

    # 万が一なにも取れなかった場合、あるいは標準的なインデックス 0～3 を補完
    existing_indices = {d[0] for d in devices}
    for idx in range(4):
        if idx not in existing_indices:
            devices.append((idx, f"[{idx}] カメラ (インデックス {idx})"))

    devices.sort(key=lambda x: x[0])
    return devices


# ---------------------------------------------------------------------------
# システム日時設定ダイアログ
# ---------------------------------------------------------------------------
class SystemDateTimeDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("本体日時設定 (システムクロック設定)")
        self.geometry("560x490")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()

        try:
            configure_modal_toplevel(self)
        except Exception:
            pass

        from datetime import datetime
        now = datetime.now()

        # 最下部ボタンエリア (side=BOTTOM で固定配置することで縦潰れを完全防止)
        f_btns = tk.Frame(self, bg=COLOR_BG_MAIN)
        f_btns.pack(side=tk.BOTTOM, fill=tk.X, pady=20, padx=24)

        def _apply():
            try:
                y = self.v_year.get()
                m = self.v_month.get()
                d = self.v_day.get()
                h = self.v_hour.get()
                mi = self.v_min.get()
                s = self.v_sec.get()
                dt_str = f"{y:04d}-{m:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"
            except Exception as ex:
                messagebox.showerror("入力エラー", f"日時の入力値が不正です:\n{ex}", parent=self)
                return

            if sys.platform.startswith("win"):
                messagebox.showinfo(
                    "日時設定 (Windows)",
                    f"Windows環境のため実際のシステム時刻変更はスキップされました。\n設定指定値: {dt_str}\n(Linux/ラズパイ環境で自動設定コマンドが実行されます)",
                    parent=self
                )
                self.destroy()
                return

            import subprocess
            cmds = [
                ["sudo", "timedatectl", "set-ntp", "false"],
                ["sudo", "timedatectl", "set-time", dt_str],
                ["sudo", "date", "-s", dt_str],
                ["sudo", "hwclock", "-w"]
            ]
            results = []
            success_count = 0
            for cmd in cmds:
                try:
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                    if res.returncode == 0:
                        success_count += 1
                        results.append(f"成功: {' '.join(cmd)}")
                    else:
                        err = res.stderr.strip() or res.stdout.strip()
                        results.append(f"失敗 ({' '.join(cmd)}): {err}")
                except Exception as ex:
                    results.append(f"エラー ({' '.join(cmd)}): {ex}")

            msg = f"日時を [{dt_str}] に設定しました。\n\n【実行詳細】\n" + "\n".join(results)
            if success_count > 0:
                messagebox.showinfo("日時設定完了", msg, parent=self)
                self.destroy()
            else:
                messagebox.showerror("日時設定失敗", msg, parent=self)

        btn_save = tk.Button(
            f_btns, text="日時を本体に反映", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_ACCENT, fg="white", relief="flat", padx=20, pady=8,
            cursor="hand2", command=_apply
        )
        btn_save.pack(side=tk.RIGHT, padx=(10, 0))

        btn_cancel = tk.Button(
            f_btns, text="キャンセル", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", padx=18, pady=8,
            cursor="hand2", command=self.destroy
        )
        btn_cancel.pack(side=tk.RIGHT)

        # ヘッダータイトル & 説明
        tk.Label(
            self, text="ラズパイ本体の日時設定", font=FONT_LARGE,
            bg=COLOR_BG_MAIN, fg=COLOR_ACCENT
        ).pack(pady=(20, 6))

        tk.Label(
            self, text="本体のシステム日付・時刻を設定します。\n(Linux / Raspberry Pi 環境で timedatectl / date が更新されます)",
            font=FONT_SET_VAL, bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB, justify="center",
            wraplength=500
        ).pack(pady=(0, 16), padx=20)

        # 入力フレーム
        f_dt = tk.Frame(self, bg=COLOR_BG_PANEL, padx=20, pady=20)
        f_dt.pack(padx=24, fill=tk.X, expand=True)

        font_num = (FONT_FAMILY, 14, "bold")
        font_lbl = (FONT_FAMILY, 12, "bold")

        # 年月日
        f_date = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_date.pack(fill=tk.X, pady=8)
        
        self.v_year = tk.IntVar(value=now.year)
        self.v_month = tk.IntVar(value=now.month)
        self.v_day = tk.IntVar(value=now.day)

        tk.Label(f_date, text="日付:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        sp_y = ttk.Spinbox(f_date, from_=2020, to=2099, increment=1, textvariable=self.v_year, width=6, font=font_num)
        sp_y.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="年", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_m = ttk.Spinbox(f_date, from_=1, to=12, increment=1, textvariable=self.v_month, width=4, font=font_num)
        sp_m.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="月", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_d = ttk.Spinbox(f_date, from_=1, to=31, increment=1, textvariable=self.v_day, width=4, font=font_num)
        sp_d.pack(side=tk.LEFT, padx=4)
        tk.Label(f_date, text="日", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)

        # 時分秒
        f_time = tk.Frame(f_dt, bg=COLOR_BG_PANEL)
        f_time.pack(fill=tk.X, pady=8)

        self.v_hour = tk.IntVar(value=now.hour)
        self.v_min = tk.IntVar(value=now.minute)
        self.v_sec = tk.IntVar(value=now.second)

        tk.Label(f_time, text="時刻:", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=6, anchor="w").pack(side=tk.LEFT)
        sp_h = ttk.Spinbox(f_time, from_=0, to=23, increment=1, textvariable=self.v_hour, width=4, font=font_num)
        sp_h.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="時", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_mi = ttk.Spinbox(f_time, from_=0, to=59, increment=1, textvariable=self.v_min, width=4, font=font_num)
        sp_mi.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="分", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT, padx=(0, 10))

        sp_s = ttk.Spinbox(f_time, from_=0, to=59, increment=1, textvariable=self.v_sec, width=4, font=font_num)
        sp_s.pack(side=tk.LEFT, padx=4)
        tk.Label(f_time, text="秒", font=font_lbl, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)

        # 全Spinboxの安全停止ハンドラ
        for sp in [sp_y, sp_m, sp_d, sp_h, sp_mi, sp_s]:
            def _stop_ttk_sp(event=None, widget=sp):
                try:
                    rep = widget.tk.call('set', '::ttk::spinbox::Repeater')
                    if rep: widget.tk.call('after', 'cancel', rep)
                except Exception:
                    pass
            sp.bind("<ButtonRelease-1>", _stop_ttk_sp, add="+")
            sp.bind("<Leave>", _stop_ttk_sp, add="+")
            sp.bind("<FocusOut>", _stop_ttk_sp, add="+")

        def _set_current():
            n = datetime.now()
            self.v_year.set(n.year)
            self.v_month.set(n.month)
            self.v_day.set(n.day)
            self.v_hour.set(n.hour)
            self.v_min.set(n.minute)
            self.v_sec.set(n.second)

        btn_now = tk.Button(
            f_dt, text="現在端末の時刻をセット", font=(FONT_FAMILY, 11, "bold"),
            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, relief="flat", padx=16, pady=6,
            cursor="hand2", command=_set_current
        )
        btn_now.pack(pady=(14, 4))


class GPIOTestDialog(tk.Toplevel):
    def __init__(self, parent, gpio_settings):
        super().__init__(parent)
        self.title("GPIO 入出力テスト")
        self.geometry("600x600")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()

        self.gpio_settings = gpio_settings
        self.running = True
        self.inputs = {}
        self.outputs = {}

        tk.Label(self, text="GPIO 入出力テスト", font=FONT_LARGE,
                 bg=COLOR_BG_MAIN, fg=COLOR_ACCENT).pack(pady=20)

        container = tk.Frame(self, bg=COLOR_BG_MAIN)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        canvas = tk.Canvas(container, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.setup_test_hardware()

        self.ui_inputs = {}

        f_in = tk.LabelFrame(scrollable_frame, text="入力テスト",
                             font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                             fg=COLOR_TEXT_MAIN, padx=20, pady=20)
        f_in.pack(fill=tk.X, padx=20, pady=10)

        for k, name in self.input_names.items():
            row = tk.Frame(f_in, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=5)
            tk.Label(row, text=name, font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                     fg=COLOR_TEXT_MAIN, width=30, anchor="w").pack(side=tk.LEFT)
            lbl_st = tk.Label(row, text="OFF", font=FONT_SET_VAL,
                              bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB, width=10)
            lbl_st.pack(side=tk.LEFT, padx=10)
            self.ui_inputs[k] = lbl_st

        f_out = tk.LabelFrame(scrollable_frame, text="出力テスト",
                              font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                              fg=COLOR_TEXT_MAIN, padx=20, pady=20)
        f_out.pack(fill=tk.X, padx=20, pady=10)

        self.output_state_ok = False
        self.output_state_ng = False

        def toggle_out(key, btn):
            if key == "ok":
                self.output_state_ok = not self.output_state_ok
                state = self.output_state_ok
            else:
                self.output_state_ng = not self.output_state_ng
                state = self.output_state_ng

            if key in self.outputs:
                if state:
                    self.outputs[key].on()
                    btn.config(text=f"{key.upper()}出力 (ON)",
                               bg=COLOR_WARNING, fg="black")
                else:
                    self.outputs[key].off()
                    btn.config(text=f"{key.upper()}出力 (OFF)",
                               bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)

        btn_f = tk.Frame(f_out, bg=COLOR_BG_PANEL)
        btn_f.pack(fill=tk.X, pady=(0, 10))

        btn_ok = tk.Button(btn_f, text="OK出力 (OFF)", font=FONT_BTN_LARGE,
                           bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                           relief="flat", width=15)
        btn_ok.pack(side=tk.LEFT, padx=10)
        btn_ok.config(command=lambda: toggle_out("ok", btn_ok))

        btn_ng = tk.Button(btn_f, text="NG出力 (OFF)", font=FONT_BTN_LARGE,
                           bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                           relief="flat", width=15)
        btn_ng.pack(side=tk.LEFT, padx=10)
        btn_ng.config(command=lambda: toggle_out("ng", btn_ng))

        tk.Label(f_out, text="(※クリックでON/OFFが切り替わります)",
                 font=FONT_NORMAL, bg=COLOR_BG_PANEL,
                 fg=COLOR_TEXT_SUB).pack(anchor="w", padx=10)

        tk.Button(self, text="閉じる", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", height=2,
                  command=self.close_test).pack(fill=tk.X, padx=20, pady=10)

        self.protocol("WM_DELETE_WINDOW", self.close_test)
        self.update_inputs()

    def setup_test_hardware(self):
        self.input_names = {}
        try:
            for t in self.gpio_settings["triggers"]:
                self.inputs[t["id"]] = DigitalInputDevice(t["pin"], pull_up=True)
                self.input_names[t["id"]] = f"トリガー: {t['name']} (ピン:{t['pin']})"
            for s in self.gpio_settings.get("pattern_pins", []):
                self.inputs[s["id"]] = DigitalInputDevice(s["pin"], pull_up=True)
                self.input_names[s["id"]] = f"パターンピン: {s['name']} (ピン:{s['pin']})"
            self.outputs["ok"] = OutputDevice(self.gpio_settings["outputs"]["ok"])
            self.outputs["ng"] = OutputDevice(self.gpio_settings["outputs"]["ng"])
        except Exception as e:
            print(f"GPIO Init Error in Test: {e}")

    def update_inputs(self):
        if not self.running or not self.winfo_exists():
            return
        for k, dev in self.inputs.items():
            if k in self.ui_inputs:
                st = dev.is_active
                lbl = self.ui_inputs[k]
                if st:
                    lbl.config(text="ON", bg=COLOR_ACCENT, fg="black")
                else:
                    lbl.config(text="OFF", bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB)
        self.after(100, self.update_inputs)

    def close_test(self):
        self.running = False
        for d in self.inputs.values():
            d.close()
        for d in self.outputs.values():
            d.close()
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.setup_hardware() # type: ignore
        self.destroy()


# ---------------------------------------------------------------------------
# 設定ダイアログ
# ---------------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, on_close_callback):
        super().__init__(parent)
        self.settings = settings
        self.on_close_callback = on_close_callback
        self.title("詳細設定 (PatchCore検査設定)")
        self.geometry("1400x900")
        self.configure(bg=COLOR_BG_MAIN)
        self.temp_data = json.loads(json.dumps(self.settings.data))
        self._sync_pattern_conditions()
        self.has_changes = False
        self._scan_status_var = tk.StringVar(value="")
        
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.preview_paused = True

        self.active_entry = (None, None) 
        self.pin_widgets = {}
        self.input_pins = {}
        self.map_labels = {}
        self.trig_scroll = tk.Frame() # type: ignore
        self.trig_list_f = tk.Frame()     # type: ignore
        self.sel_list_f = tk.Frame()      # type: ignore
        self.lbl_gpio_status = tk.Label() # type: ignore
        self.cam_body = tk.Frame()  # type: ignore
        self.pat_body = tk.Frame()  # type: ignore
        self.lb_pat = tk.Listbox()  # type: ignore
        
        self.v_ok = tk.IntVar(value=self.temp_data["gpio"]["outputs"]["ok"])
        self.v_ng = tk.IntVar(value=self.temp_data["gpio"]["outputs"]["ng"])

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TNotebook", background=COLOR_BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=COLOR_BG_PANEL,
                        foreground=COLOR_TEXT_MAIN, font=FONT_SET_TAB,
                        padding=[20, 10], focuscolor=COLOR_BG_MAIN)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_ACCENT)],
                  foreground=[("selected", "black")])

        btn_f = tk.Frame(self, pady=20, bg=COLOR_BG_MAIN)
        btn_f.pack(side=tk.BOTTOM, fill=tk.X, padx=20)
        
        self.btn_save = tk.Button(btn_f, text="保存して閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
                                  fg="white", relief="flat", width=22,
                                  command=self.save_and_close)
        self.btn_save.pack(side=tk.RIGHT, padx=5)
        
        tk.Button(btn_f, text="キャンセル", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", width=10,
                  command=self.on_cancel).pack(side=tk.RIGHT, padx=5)

        nb = ttk.Notebook(self)
        nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=20)

        self.t_cam = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_cam, text=" カメラ ")
        self.t_gpio = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_gpio, text=" GPIOピン ")
        self.t_pat = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_pat, text=" パターン ")
        self.t_res = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_res, text=" 画素数 ")
        self.t_sys = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_sys, text=" システム ")

        self.setup_cam()
        self.setup_gpio()
        self.setup_pat()
        self.setup_res()
        self.setup_sys()

        btn_help = tk.Button(btn_f, text="ヘルプ", font=FONT_SET_LBL,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", command=self.show_settings_help)
        btn_help.pack(side=tk.LEFT, padx=20)

        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.option_add("*TCombobox*Listbox.font", FONT_SET_VAL)

        configure_modal_toplevel(self, parent)

    def on_cancel(self):
        release_modal_toplevel(self)
        if hasattr(self, "_live_preview_win") and self._live_preview_win.winfo_exists():
            self._live_preview_win.destroy()
        if self.on_close_callback:
            self.on_close_callback()
        
        if hasattr(self.master, "app_instance"):
            self.master.app_instance.preview_paused = False # type: ignore

        self.destroy()

    def show_settings_help(self):
        help_data = {
            "1. カメラ設定": "【概要】使用するUSBカメラの接続と名前付けを行います。\n"
                           "・インデックス: カメラの識別番号です。\n"
                           "・表示名: メイン画面や履歴で表示されるカメラの名称です。\n"
                           "・カメラ検索: 接続されているカメラを自動で探し、リストへ追加・自動割り当てします。\n"
                           "・テストボタン: 現在のインデックスで正常に映るか、ライブ映像で確認できます。",
            "2. GPIOピン設定": "【概要】Raspberry PiのGPIOピンへの配線設定です。\n"
                            "・トリガー: 検査を起動する入力ピンです。リスト上から順に入力待ちとなり、順番通りに入力された場合のみ有効です。\n"
                            "・パターン判定ピン: どの検査パターンを使うかをピンのON/OFFで決めます。\n"
                            "・出力(OK/NG): 判定結果を外部装置（PLC等）へ送る出力ピンです。\n"
                            "・40Pin Map: Raspberry Piの配線図を参照できます。クリックでBCM番号を入力できます。",
            "3. パターン設定": "【概要】PatchCore / PaDiM (異常検知)モデルの設定を行います。\n"
                           "・各カメラ行ごとに、学習済みのモデルファイル（`.ckpt`）を割り当てます。PatchCore および PaDiM の両モデルに対応しています。未指定のカメラは自動的に検査SKIP（評価対象外）となります。\n"
                           "・「判定しきい値」にはそのモデルで使用する異常判定しきい値を設定します (通常 0.40〜0.50 程度)。\n"
                           "・「テスト」ボタンを押すことで、その設定値を用いて指定したカメラでリアルタイムヒートマップ判定テストを行うことができます。\n",
            "4. 保存・画素数設定": "【概要】画像の質や保存先、保存ルールを決めます。\n"
                         "・撮影解像度: カメラから読み出す際の土台のサイズです。大きいほどAIの精度が上がる可能性がありますが、遅くなります。\n"
                         "・各判定画像の保存サイズ: 保存時の大きさを決めます。「保存しない」を選ぶと画像が残りません。",
            "5. システム設定": "【概要】システムの全般設定です。\n"
                            "・結果出力先: ログ、CSV、画像一式を保存する親フォルダの場所を絶対パスで指定します。\n"
                            "・最大リトライ: 1回のトリガーで最大何回まで撮り直すか。\n"
                            "・結果表示時間: 判定後、その画像を画面に表示し続ける秒数です。\n"
                            "・OK/NG出力時間: 信号を何秒間出し続けるかです。NGを空欄にするか「ブザー停止まで保持」チェックボックスをONにすると停止ボタンが押されるまで保持します。\n"
                            "・自動削除有効: 容量上限を超えた際、古い画像から順に自動削除します。CSVログは削除されません。"
        }
        HelpWindow(self, "詳細設定 操作ガイド", help_data)

    def _entry(self, parent, var, width=None, key_path=None):
        ent = tk.Entry(parent, textvariable=var, font=FONT_SET_VAL,
                        width=width, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                        insertbackground="white", relief="flat")
        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return ent

    def _spinbox(self, parent, var, from_, to, increment=1, width=6, key_path=None):
        sb = tk.Spinbox(parent, from_=from_, to=to, increment=increment, textvariable=var,
                        font=FONT_SET_VAL, width=width, bg=COLOR_BG_INPUT, fg="white", 
                        buttonbackground="#78909C", bd=1, relief="solid",
                        repeatdelay=0, repeatinterval=0)
        
        # ラズパイ環境での長押しタイマー暴走を防止する安全ハンドラ
        def _stop_repeat(event=None):
            try:
                rep_id = sb.tk.call('set', '::tk::spinbox::Repeater')
                if rep_id:
                    sb.tk.call('after', 'cancel', rep_id)
            except Exception:
                pass
        sb.bind("<ButtonRelease-1>", _stop_repeat, add="+")
        sb.bind("<Leave>", _stop_repeat, add="+")
        sb.bind("<FocusOut>", _stop_repeat, add="+")

        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return sb

    def create_scrollable_panel(self, parent):
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if not self.winfo_exists(): return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return scrollable_frame

    # ---- カメラタブ ----
    def setup_cam(self):
        outer, inner = create_card(self.t_cam, "カメラ設定 (1-4台)")
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        self.cam_body = tk.Frame(inner, bg=COLOR_BG_PANEL)
        self.cam_body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        f_bottom = tk.Frame(inner, bg=COLOR_BG_PANEL)
        f_bottom.pack(fill=tk.X, pady=(0, 10))
        btn_add = tk.Button(f_bottom, text="+ カメラ追加", font=FONT_BTN_LARGE,
                  bg=COLOR_ACCENT, fg="black", relief="flat",
                  command=self.add_cam)
        btn_add.pack(side=tk.LEFT)
        Tooltip(btn_add, "新しいカメラ設定を追加します。")
        
        btn_scan = tk.Button(f_bottom, text="接続カメラを自動検出", font=FONT_BTN_LARGE,
                  bg="#546E7A", fg="white", relief="flat",
                  command=self.scan_cameras)
        btn_scan.pack(side=tk.LEFT, padx=(10, 0))
        Tooltip(btn_scan, "OSが認識しているカメラデバイスをスキャンし、利用可能なカメラ名とインデックスを取得します")
        tk.Label(f_bottom, textvariable=self._scan_status_var, font=FONT_NORMAL,
                 bg=COLOR_BG_PANEL, fg=COLOR_WARNING).pack(side=tk.LEFT, padx=15)
        
        # 最初は即座にUIを構築（フリーズ防止）
        self.available_cams = [(c.get("index", 0), c.get("device_name", f"[{c.get('index', 0)}] カメラ")) for c in self.temp_data.get("cameras", [])]
        if not self.available_cams:
            self.available_cams = [(idx, f"[{idx}] カメラ (インデックス {idx})") for idx in range(4)]
        self.refresh_cam()

        # バックグラウンドスレッドで非同期にカメラデバイスを探索
        def _async_init_scan():
            cams = detect_available_cameras()
            def _apply():
                if self.winfo_exists():
                    self.available_cams = cams
                    self.refresh_cam()
            self.after(0, _apply)

        threading.Thread(target=_async_init_scan, daemon=True).start()

    def refresh_cam(self):
        for w in self.cam_body.winfo_children():
            w.destroy()

        cam_labels = [label for _, label in self.available_cams]

        for i, c in enumerate(self.temp_data["cameras"]):
            def _create_cam_row(idx=i, cam_obj=c):
                f = tk.LabelFrame(self.cam_body, text=f"カメラ {idx+1}",
                                  font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                                  fg=COLOR_TEXT_SUB, padx=10, pady=10,
                                  relief="solid", bd=1)
                f.pack(fill=tk.X, pady=5)
                l_name = tk.Label(f, text="表示名:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                         fg=COLOR_TEXT_MAIN)
                l_name.grid(row=0, column=0)
                Tooltip(l_name, "カメラの識別表示名です。")
                vn = tk.StringVar(value=cam_obj["name"])
                e_name = self._entry(f, vn, key_path=f"cameras.{idx}.name")
                e_name.grid(row=0, column=1, padx=10)

                l_idx = tk.Label(f, text="カメラデバイス:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                         fg=COLOR_TEXT_MAIN)
                l_idx.grid(row=0, column=2)
                Tooltip(l_idx, "接続されているカメラデバイスを選択します。")
                
                curr_idx = cam_obj.get("index", 0)
                # 初期選択ラベルの探索
                init_val = f"[{curr_idx}] カメラ"
                for c_int, c_label in self.available_cams:
                    if c_int == curr_idx:
                        init_val = c_label
                        break

                combo_var = tk.StringVar(value=init_val)
                cb_dev = ttk.Combobox(f, textvariable=combo_var, values=cam_labels,
                                      font=FONT_SET_VAL, state="normal", width=28)
                cb_dev.grid(row=0, column=3, padx=10)

                def _upd_inner(*args, v_n=vn, v_c=combo_var):
                    sel_text = v_c.get()
                    # 選ばれたテキストからインデックス数値を判定 (例: "[0] Integrated Camera" -> 0)
                    val = 0
                    if sel_text.startswith("[") and "]" in sel_text:
                        try:
                            val = int(sel_text.split("]")[0].replace("[", ""))
                        except ValueError:
                            val = 0
                    else:
                        try:
                            val = int(sel_text)
                        except ValueError:
                            val = 0
                    self.temp_data["cameras"][idx].update({
                        "name": v_n.get(),
                        "index": val,
                        "device_name": sel_text
                    })
                    self._mark_changed()

                vn.trace_add("write", _upd_inner)
                combo_var.trace_add("write", _upd_inner)
                cb_dev.bind("<<ComboboxSelected>>", _upd_inner)

                if len(self.temp_data["cameras"]) > 1:
                    tk.Button(f, text="削除", font=FONT_BTN_LARGE, bg=COLOR_NG_MUTED,
                              fg="white", relief="flat",
                              command=lambda: self.del_cam(idx)).grid(row=0, column=4, padx=10)

                tk.Button(f, text="テスト", font=FONT_BTN_LARGE, bg=COLOR_ACCENT,
                          fg="black", relief="flat",
                          command=lambda: self.test_camera(idx)).grid(row=0, column=5, padx=10)
            
            _create_cam_row()

    def test_camera(self, idx):
        c_idx_str = self.temp_data["cameras"][idx].get("index", 0)
        try:
            c_idx = int(c_idx_str)
        except ValueError:
            messagebox.showerror("エラー", "正しいカメラインデックスを選択してください。")
            return
        dev_name = self.temp_data["cameras"][idx].get("device_name", f"インデックス: {c_idx}")
        test_win = tk.Toplevel(self)
        test_win.title(f"カメラテスト ({dev_name})")
        test_win.geometry("640x480")
        test_win.transient(self)
        test_win.grab_set()
        lbl = tk.Label(test_win, bg="black")
        lbl.pack(fill=tk.BOTH, expand=True)
        cap = cv2.VideoCapture(c_idx)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            messagebox.showerror("エラー", f"カメラ ({dev_name}) を開けませんでした。")
            test_win.destroy()
            return

        def update_frame():
            if not test_win.winfo_exists():
                cap.release()
                return
            ret, frame = cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(frame)
                img = img.resize((640, 480))
                photo = ImageTk.PhotoImage(image=img)
                lbl.config(image=photo)
                lbl.image = photo
            else:
                lbl.config(text="フレームを取得できません", fg="white")
            test_win.after(30, update_frame)

        update_frame()

    def add_cam(self):
        if len(self.temp_data["cameras"]) < 4:
            next_num = len(self.temp_data["cameras"]) + 1
            default_idx = 0
            if self.available_cams:
                used_indices = {c.get("index") for c in self.temp_data["cameras"]}
                for c_int, _ in self.available_cams:
                    if c_int not in used_indices:
                        default_idx = c_int
                        break
            self.temp_data["cameras"].append({
                "id": f"cam_{int(time.time())}",
                "name": f"カメラ {next_num}",
                "index": default_idx
            })
            self.refresh_cam()
            self._mark_changed()

    def del_cam(self, idx):
        self.temp_data["cameras"].pop(idx)
        self.refresh_cam()
        self._mark_changed()

    def scan_cameras(self):
        self._scan_status_var.set("スキャン中...")

        def _do_scan():
            cams = detect_available_cameras()
            self.after(0, lambda: _on_found(cams))

        def _on_found(cams):
            if not self.winfo_exists():
                return
            self.available_cams = cams
            self._scan_status_var.set(f"検出: {len(cams)}台")
            if not cams:
                messagebox.showinfo("カメラ検出", "利用可能なカメラが検出されませんでした。")
                self.refresh_cam()
                return
            win = tk.Toplevel(self)
            win.title("検出されたカメラ一覧")
            win.geometry("480x360")
            win.configure(bg=COLOR_BG_MAIN)
            win.transient(self)
            win.grab_set()
            tk.Label(win, text="検出された以下のカメラを選択して一括追加できます:",
                     font=FONT_NORMAL, bg=COLOR_BG_MAIN, fg=COLOR_TEXT_MAIN,
                     wraplength=440).pack(pady=(15, 5), padx=15)
            vars_list = []
            for cidx, clabel in cams:
                v = tk.BooleanVar(value=True)
                cb = tk.Checkbutton(win, text=clabel, font=FONT_SET_VAL,
                                    variable=v, bg=COLOR_BG_MAIN, fg=COLOR_TEXT_MAIN,
                                    selectcolor=COLOR_BG_INPUT, activebackground=COLOR_BG_MAIN,
                                    relief="flat")
                cb.pack(anchor="w", padx=30, pady=4)
                vars_list.append((cidx, clabel, v))

            def _apply():
                current_indices = {c.get("index") for c in self.temp_data["cameras"]}
                for cidx, clabel, v in vars_list:
                    if v.get() and cidx not in current_indices:
                        if len(self.temp_data["cameras"]) < 4:
                            next_n = len(self.temp_data["cameras"]) + 1
                            self.temp_data["cameras"].append({
                                "id": f"cam_{int(time.time())}_{cidx}",
                                "name": f"カメラ {next_n}",
                                "index": cidx,
                                "device_name": clabel
                            })
                self.refresh_cam()
                win.destroy()

            tk.Button(win, text="選択を追加", font=FONT_BOLD, bg=COLOR_OK,
                      fg="black", relief="flat", command=_apply).pack(pady=10)
            tk.Button(win, text="キャンセル", font=FONT_NORMAL, bg=COLOR_BG_INPUT,
                      fg=COLOR_TEXT_MAIN, relief="flat", command=win.destroy).pack()

        threading.Thread(target=_do_scan, daemon=True).start()

    def _mark_changed(self, *args):
        if not self.has_changes:
            self.has_changes = True
            if hasattr(self, "btn_save") and self.btn_save.winfo_exists():
                self.btn_save.config(bg=COLOR_OK, fg="black", text="変更を適用して保存")

    # ---- GPIO設定 ----
    def setup_gpio(self):
        self.active_entry = (None, None) 
        self.pin_widgets = {}     
        self.input_pins = {}      

        main_f = tk.Frame(self.t_gpio, bg=COLOR_BG_MAIN)
        main_f.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        col_left = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        col_mid = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_mid.pack(side=tk.LEFT, fill=tk.Y, padx=5)

        col_right = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        self.trig_scroll = self.create_scrollable_panel(col_left)
        outer_t, inner_t = create_card(self.trig_scroll, "トリガー入力")
        outer_t.pack(fill=tk.X, pady=(0, 10))
        self.trig_list_f = tk.Frame(inner_t, bg=COLOR_BG_PANEL)
        self.trig_list_f.pack(fill=tk.X)
        btn_add_t = tk.Button(inner_t, text="+ 追加", font=FONT_BTN_LARGE, bg=COLOR_ACCENT, fg="black", relief="flat", command=self.add_trig)
        btn_add_t.pack(anchor="e", pady=5)
        Tooltip(btn_add_t, "新しいトリガー入力ピンを追加します。")

        outer_s, inner_s = create_card(self.trig_scroll, "パターン切替")
        outer_s.pack(fill=tk.X, pady=10)
        self.sel_list_f = tk.Frame(inner_s, bg=COLOR_BG_PANEL)
        self.sel_list_f.pack(fill=tk.X)
        btn_add_s = tk.Button(inner_s, text="+ 追加", font=FONT_BTN_LARGE, bg=COLOR_ACCENT, fg="black", relief="flat", command=self.add_sel_pin)
        btn_add_s.pack(anchor="e", pady=5)
        Tooltip(btn_add_s, "パターンを切り替えるための入力ピンを追加します。")

        f_mid_inner = tk.Frame(col_mid, bg=COLOR_BG_MAIN)
        f_mid_inner.pack(fill=tk.BOTH, expand=True)
        self.show_gpio_map(f_mid_inner)

        outer_out, inner_out = create_card(col_right, "判定出力")
        outer_out.pack(fill=tk.X, pady=(0, 10))
        
        f_out = tk.Frame(inner_out, bg=COLOR_BG_PANEL)
        f_out.pack(fill=tk.X)

        def _make_out_row(parent, label, var, row, key):
            tk.Label(parent, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).grid(row=row, column=0, pady=10, sticky="w")
            e = self._entry(parent, var, width=5, key_path=f"gpio.outputs.{key}")
            e.grid(row=row, column=1, padx=10)
            e.bind("<FocusIn>", lambda ev: self._set_active_entry(e, var))
            
            btn = tk.Button(parent, text="テスト点灯", font=FONT_NORMAL, bg="#546E7A", fg="white", relief="flat")
            btn.grid(row=row, column=2, padx=5)
            
            led = tk.Canvas(parent, width=20, height=20, bg=COLOR_BG_PANEL, highlightthickness=0)
            led.grid(row=row, column=3, padx=5)
            circle = led.create_oval(2, 2, 18, 18, fill="#333", outline="#555")
            
            def _toggle_test(v=var, l=led, c=circle, b=btn):
                app = getattr(self.master, "app_instance", None)
                if not app: return
                pin = 0
                try: pin = int(v.get())
                except: return
                
                cur_color = l.itemcget(c, "fill")
                if cur_color == "#333":
                    l.itemconfig(c, fill=COLOR_OK)
                else:
                    l.itemconfig(c, fill="#333")
            
            btn.config(command=_toggle_test)

        _make_out_row(f_out, "OK出力:", self.v_ok, 0, "ok")
        Tooltip(f_out.grid_slaves(row=0, column=0)[0], "OK判定時に信号を出すGPIOピンです。")
        _make_out_row(f_out, "NG出力:", self.v_ng, 1, "ng")
        Tooltip(f_out.grid_slaves(row=1, column=0)[0], "NG判定時に信号を出すGPIOピンです。")

        outer_st, inner_st = create_card(col_right, "システム状態")
        outer_st.pack(fill=tk.X, pady=10)
        self.lbl_gpio_status = tk.Label(inner_st, text="GPIO接続確認中...", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
        self.lbl_gpio_status.pack(pady=10)

        self.refresh_gpio_trig()
        self.refresh_gpio_sel()
        self._check_gpio_connection()
        self._start_monitoring()

    def _set_active_entry(self, entry, var):
        self.active_entry = (entry, var)

    def _check_gpio_connection(self):
        from .hardware import GPIO_AVAILABLE
        if GPIO_AVAILABLE:
            self.lbl_gpio_status.config(text="GPIO: 接続済み", fg=COLOR_OK)
        else:
            self.lbl_gpio_status.config(text="GPIO: モック動作中", fg=COLOR_WARNING)

    def _start_monitoring(self):
        if not self.winfo_exists():
            return
        if not hasattr(self, "t_gpio") or not self.t_gpio.winfo_exists():
            return

        app = getattr(self.master, "app_instance", None)
        app_inputs = getattr(app, "inputs", {}) # type: ignore
        if app_inputs:
            for t in self.temp_data["gpio"]["triggers"]:
                tid = t["id"]
                if tid in app_inputs and tid in self.pin_widgets:
                    state = app_inputs[tid].is_active
                    led, circle = self.pin_widgets[tid]
                    led.itemconfig(circle, fill=COLOR_OK if state else "#333")
            
            for s in self.temp_data["gpio"].get("pattern_pins", []):
                sid = f"sel_{s['id']}"
                if sid in app_inputs and sid in self.pin_widgets:
                    state = app_inputs[sid].is_active
                    led, circle = self.pin_widgets[sid]
                    led.itemconfig(circle, fill=COLOR_OK if state else "#333")

        self.after(200, self._start_monitoring)

    def show_gpio_map(self, parent):
        outer, inner = create_card(parent, "Pi 40Pin Map")
        outer.pack(fill=tk.BOTH, expand=True)

        def _on_pin_clicked(bcm_val):
            widget, var = getattr(self, "active_entry", (None, None))
            if widget and var and bcm_val is not None:
                var.set(bcm_val)
                widget.focus_set()

        pins = [
            (1, "3.3V", None),   (2, "5V", None),
            (3, "GPIO 2", 2),    (4, "5V", None),
            (5, "GPIO 3", 3),    (6, "GND", None),
            (7, "GPIO 4", 4),    (8, "GPIO 14", 14),
            (9, "GND", None),    (10, "GPIO 15", 15),
            (11, "GPIO 17", 17), (12, "GPIO 18", 18),
            (13, "GPIO 27", 27), (14, "GND", None),
            (15, "GPIO 22", 22), (16, "GPIO 23", 23),
            (17, "3.3V", None),  (18, "GPIO 24", 24),
            (19, "GPIO 10", 10), (20, "GND", None),
            (21, "GPIO 9", 9),   (22, "GPIO 25", 25),
            (23, "GPIO 11", 11), (24, "GPIO 8", 8),
            (25, "GND", None),   (26, "GPIO 7", 7),
            (27, "ID_SD", None), (28, "ID_SC", None),
            (29, "GPIO 5", 5),   (30, "GND", None),
            (31, "GPIO 6", 6),   (32, "GPIO 12", 12),
            (33, "GPIO 13", 13), (34, "GND", None),
            (35, "GPIO 19", 19), (36, "GPIO 16", 16),
            (37, "GPIO 26", 26), (38, "GPIO 20", 20),
            (39, "GND", None),   (40, "GPIO 21", 21)
        ]

        mf = tk.Frame(inner, bg=COLOR_BG_PANEL)
        mf.pack(pady=15, padx=20) 

        for i, (pno, name, bcm) in enumerate(pins):
            col_idx = 0 if i % 2 == 0 else 2
            row_idx = i // 2
            
            lbl_no = tk.Label(mf, text=str(pno), font=(FONT_FAMILY, 10, "bold"),
                              width=3, bg="#222", fg="white")
            
            lbl_color = "#444"
            if "V" in name: lbl_color = "#8D6E63"   
            if "GND" in name: lbl_color = "#212121"  
            
            lbl_name = tk.Label(mf, text=name, font=(FONT_FAMILY, 10),
                                width=12, bg=lbl_color, fg=COLOR_TEXT_MAIN,
                                padx=5, pady=3, relief="flat")

            if i % 2 == 0:  
                lbl_no.grid(row=row_idx, column=0, padx=2, pady=1)
                lbl_name.grid(row=row_idx, column=1, padx=(2, 10), pady=1, sticky="w")
            else:  
                lbl_name.grid(row=row_idx, column=2, padx=(10, 2), pady=1, sticky="e")
                lbl_no.grid(row=row_idx, column=3, padx=2, pady=1)

            if bcm is not None:
                def make_handler(b=bcm): return lambda e: _on_pin_clicked(b)
                lbl_no.bind("<Button-1>", make_handler())
                lbl_name.bind("<Button-1>", make_handler())
                lbl_no.config(cursor="hand2")
                lbl_name.config(cursor="hand2")
                Tooltip(lbl_name, "クリックで選択中の入力欄にセットします。")

    def refresh_gpio_trig(self):
        for w in self.trig_list_f.winfo_children(): w.destroy()
        
        for i, t in enumerate(self.temp_data["gpio"]["triggers"]):
            def _create_trig_row(idx=i, trig_obj=t):
                f = tk.Frame(self.trig_list_f, bg=COLOR_BG_PANEL)
                f.pack(fill=tk.X, pady=2)
                
                led = tk.Canvas(f, width=16, height=16, bg=COLOR_BG_PANEL, highlightthickness=0)
                led.pack(side=tk.LEFT, padx=5)
                circle = led.create_oval(2, 2, 14, 14, fill="#333", outline="#555")
                
                vn = tk.StringVar(value=trig_obj["name"])
                l_trig = tk.Label(f, text="トリガー名:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
                l_trig.pack(side=tk.LEFT, padx=(5, 2))
                self._entry(f, vn, width=12, key_path=f"gpio.triggers.{idx}.name").pack(side=tk.LEFT, padx=2)
                
                vp = tk.IntVar(value=trig_obj["pin"])
                l_pin = tk.Label(f, text=" Pin:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
                l_pin.pack(side=tk.LEFT, padx=(5, 2))
                p_ent = self._entry(f, vp, width=4, key_path=f"gpio.triggers.{idx}.pin")
                p_ent.pack(side=tk.LEFT, padx=5)
                p_ent.bind("<FocusIn>", lambda ev, e=p_ent, v=vp: self._set_active_entry(e, v))
                
                self.pin_widgets[trig_obj["id"]] = (led, circle)

                def _upd_trig_inner(v1=vn, v2=vp):
                    try:
                        self.temp_data["gpio"]["triggers"][idx].update({"name": v1.get(), "pin": v2.get()})
                    except tk.TclError:
                        pass  
                
                vn.trace_add("write", lambda *a: _upd_trig_inner())
                vp.trace_add("write", lambda *a: _upd_trig_inner())

                if len(self.temp_data["gpio"]["triggers"]) > 1:
                    tk.Button(f, text="×", font=(FONT_FAMILY, 10, "bold"), bg=COLOR_NG_MUTED, fg="white", relief="flat", width=2,
                              command=lambda: [self.temp_data["gpio"]["triggers"].pop(idx), self.refresh_gpio_trig(), self._mark_changed()]).pack(side=tk.RIGHT)
            
            _create_trig_row()

    def refresh_gpio_sel(self):
        for w in self.sel_list_f.winfo_children(): w.destroy()
        
        for i, s in enumerate(self.temp_data["gpio"].get("pattern_pins", [])):
            f = tk.Frame(self.sel_list_f, bg=COLOR_BG_PANEL)
            f.pack(fill=tk.X, pady=2)
            
            led = tk.Canvas(f, width=16, height=16, bg=COLOR_BG_PANEL, highlightthickness=0)
            led.pack(side=tk.LEFT, padx=5)
            circle = led.create_oval(2, 2, 14, 14, fill="#333", outline="#555")
            
            vn = tk.StringVar(value=s["name"])
            l_trig = tk.Label(f, text="名称:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
            l_trig.pack(side=tk.LEFT, padx=(5, 2))
            self._entry(f, vn, width=12, key_path=f"gpio.pattern_pins.{i}.name").pack(side=tk.LEFT, padx=2)
            
            vp = tk.IntVar(value=s["pin"])
            l_pin = tk.Label(f, text=" Pin:", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
            l_pin.pack(side=tk.LEFT, padx=(5, 2))
            p_ent = self._entry(f, vp, width=4, key_path=f"gpio.pattern_pins.{i}.pin")
            p_ent.pack(side=tk.LEFT, padx=5)
            p_ent.bind("<FocusIn>", lambda ev, e=p_ent, v=vp: self._set_active_entry(e, v))

            self.pin_widgets[f"sel_{s['id']}"] = (led, circle)

            def _upd_sel(*args, idx=i, name_var=vn, pin_var=vp):
                try:
                    self.temp_data["gpio"]["pattern_pins"][idx].update({"name": name_var.get(), "pin": pin_var.get()})
                except tk.TclError:
                    pass  
            vn.trace_add("write", _upd_sel)
            vp.trace_add("write", _upd_sel)

            if len(self.temp_data["gpio"].get("pattern_pins", [])) > 1:
               tk.Button(f, text="×", font=(FONT_FAMILY, 10, "bold"), bg=COLOR_NG_MUTED, fg="white", relief="flat", width=2,
                         command=lambda idx=i: [self.temp_data["gpio"]["pattern_pins"].pop(idx), self.refresh_gpio_sel(), self._mark_changed()]).pack(side=tk.RIGHT)

    def add_trig(self):
        self.temp_data["gpio"]["triggers"].append({"id": f"t_{int(time.time())}", "name": f"トリガー {len(self.temp_data['gpio']['triggers'])+1}", "pin": 0})
        self.refresh_gpio_trig()
        self._mark_changed()

    def add_sel_pin(self):
        self.temp_data["gpio"]["pattern_pins"].append({"id": f"s_{int(time.time())}", "name": f"ピン {len(self.temp_data['gpio']['pattern_pins'])+1}", "pin": 0})
        self.refresh_gpio_sel()
        self._mark_changed()

    # ---- パターン設定タブ (PatchCore用個別カメラモデル設定仕様) ----
    def setup_pat(self):
        m = tk.Frame(self.t_pat, bg=COLOR_BG_MAIN)
        m.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        left_outer, left = create_card(m, "パターン一覧")
        left_outer.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))
        left_outer.config(width=350)

        l_pat = tk.Label(left, text="パターン一覧", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
        l_pat.pack(anchor="w", padx=10, pady=(5, 0))
        self.lb_pat = tk.Listbox(left, font=FONT_SET_LBL, bg=COLOR_BG_INPUT,
                                 fg=COLOR_TEXT_MAIN, selectbackground=COLOR_ACCENT,
                                 selectforeground="black", relief="flat",
                                 exportselection=False)
        self.lb_pat.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.lb_pat.bind("<<ListboxSelect>>", self.on_pat_sel)

        tk.Button(left, text="+ パターン追加", font=FONT_BTN_LARGE,
                  bg=COLOR_ACCENT, fg="black", relief="flat",
                  command=self.add_pat).pack(fill=tk.X, padx=10, pady=5)
        tk.Button(left, text="削除", font=FONT_BTN_LARGE, bg=COLOR_NG_MUTED,
                  fg="white", relief="flat",
                  command=self.del_pat).pack(fill=tk.X, padx=10, pady=5)

        right_outer, p_body_container = create_card(m, "パターン設定")
        right_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.pat_canvas, self.pat_body = self._create_pat_scrollable_panel(p_body_container)
        self.refresh_pat_list()
        self.after(100, self._auto_select_first_pat)

    def _create_pat_scrollable_panel(self, parent):
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)
        
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if not self.winfo_exists(): return
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        return canvas, scrollable_frame

    def _auto_select_first_pat(self):
        if not self.winfo_exists(): return
        if self.lb_pat.size() > 0:
            self.lb_pat.selection_set(0)
            self.on_pat_sel(None)

    def refresh_pat_list(self):
        self.lb_pat.delete(0, tk.END)
        for pid in self.temp_data["pattern_order"]:
            self.lb_pat.insert(tk.END, self.temp_data["patterns"][pid]["name"])

    def add_pat(self):
        pid = f"p_{int(time.time())}"
        next_num = len(self.temp_data['pattern_order']) + 1
        name = f"パターン {next_num}"
        self.temp_data["patterns"][pid] = {
            "name": name,
            "pin_condition": [0] * len(self.temp_data["gpio"].get("pattern_pins", [])),
            "stages": {}
        }
        self.temp_data["pattern_order"].append(pid)
        self.refresh_pat_list()
        self._mark_changed()

    def del_pat(self):
        s = self.lb_pat.curselection()
        if s:
            pid = self.temp_data["pattern_order"].pop(s[0])
            del self.temp_data["patterns"][pid]
            self.refresh_pat_list()
            for w in self.pat_body.winfo_children():
                w.destroy()
            self.after(50, self._auto_select_first_pat)
            self._mark_changed()

    def on_pat_sel(self, e):
        y_pos = 0.0
        if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists():
            y_pos = self.pat_canvas.yview()[0]

        for w in self.pat_body.winfo_children():
            w.destroy()
        s = self.lb_pat.curselection()
        if not s:
            return
        pid = self.temp_data["pattern_order"][s[0]]
        p = self.temp_data["patterns"][pid]

        # 1. 基本設定カード (名称・ピン条件)
        outer1, inner1 = create_card(self.pat_body, "基本設定")
        outer1.pack(fill=tk.X, pady=(0, 15))

        l_name = tk.Label(inner1, text="名称:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        l_name.pack(anchor="w")
        vn = tk.StringVar(value=p["name"])
        e_name = self._entry(inner1, vn, key_path=f"patterns.{pid}.name")
        e_name.pack(fill=tk.X, pady=(5, 15))
        vn.trace_add("write", lambda *a: p.update({"name": vn.get()}))

        l_pin = tk.Label(inner1, text="パターン信号条件:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        l_pin.pack(anchor="w")
        
        pins = self.temp_data["gpio"].get("pattern_pins", [])
        if len(p["pin_condition"]) != len(pins):
            cond = p["pin_condition"]
            if len(cond) < len(pins):
                cond = cond + [0] * (len(pins) - len(cond))
            else:
                cond = cond[:len(pins)]
            p["pin_condition"] = cond

        p_grid = tk.Frame(inner1, bg=COLOR_BG_PANEL)
        p_grid.pack(anchor="w", pady=5)
        p_vars = []
        for i, pin in enumerate(pins):
            def _create_pin_ui(idx=i, pin_obj=pin):
                v = tk.IntVar(value=p["pin_condition"][idx])
                p_vars.append(v)
                btn = tk.Button(p_grid, font=FONT_SET_VAL, width=4, relief="flat")

                def _toggle(var=v, b=btn, i_idx=idx):
                    var.set(1 if var.get() == 0 else 0)
                    _upd_btn_color(b, var.get(), i_idx)
                    self._mark_changed()

                def _upd_btn_color(b, val, b_idx):
                    if val == 1:
                        b.config(text="ON", bg=COLOR_ACCENT, fg="black")
                    else:
                        b.config(text="OFF", bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)

                l_p = tk.Label(p_grid, text=f"{pin_obj['name']}:", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
                l_p.grid(row=idx // 3, column=(idx % 3) * 2, sticky="e", padx=(10, 2))

                btn.config(command=_toggle)
                _upd_btn_color(btn, v.get(), idx)
                btn.grid(row=idx // 3, column=(idx % 3) * 2 + 1, padx=(0, 10), pady=5)
            
            _create_pin_ui()

        def _upd_p_pins(*a):
            try:
                p["pin_condition"] = [var.get() for var in p_vars]
            except tk.TclError:
                pass
        for v in p_vars:
            v.trace_add("write", _upd_p_pins)

        # 2. トリガー別 PatchCore判定条件 (カメラ別固定割り当て仕様)
        tk.Label(self.pat_body, text="トリガー別 判定モデル・しきい値設定", font=FONT_SET_LBL,
                 bg=COLOR_BG_MAIN, fg=COLOR_ACCENT).pack(anchor="w", pady=(10, 5))

        is_half_step = bool(self.temp_data.get("system", {}).get("commit_half_step", False))

        def _create_trigger_card(t):
            tid = t["id"]
            if tid not in p["stages"]:
                p["stages"][tid] = {"conditions": {}}
            st = p["stages"][tid]

            # ドアライン対応時、conditions_fr / conditions_rr が未作成なら初期化
            if is_half_step:
                if "conditions_fr" not in st:
                    st["conditions_fr"] = copy.deepcopy(st.get("conditions", {}))
                if "conditions_rr" not in st:
                    st["conditions_rr"] = copy.deepcopy(st.get("conditions", {}))
            
            cf_outer = tk.Frame(self.pat_body, bg="#808080", padx=1, pady=1)
            cf_outer.pack(fill=tk.X, pady=8)
            cf_inner = tk.Frame(cf_outer, bg=COLOR_BG_PANEL, padx=15, pady=10)
            cf_inner.pack(fill=tk.BOTH, expand=True)

            head_f = tk.Frame(cf_inner, bg=COLOR_BG_PANEL)
            head_f.pack(fill=tk.X)
            tk.Label(head_f, text=f"■ {t['name']}", font=FONT_BOLD,
                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)
            
            cond_container = tk.Frame(cf_inner, bg=COLOR_BG_PANEL)

            def _build_table(target_frame, part="fr"):
                for w in target_frame.winfo_children():
                    w.destroy()

                cond_key = "conditions"
                if is_half_step:
                    cond_key = "conditions_fr" if part == "fr" else "conditions_rr"

                # PatchCore用の構造調整（辞書形式）
                if not isinstance(st.get(cond_key), dict):
                    st[cond_key] = {}
                
                # テーブルヘッダー
                header_f = tk.Frame(target_frame, bg=COLOR_BG_PANEL)
                header_f.pack(fill=tk.X, pady=(0, 5))
                
                tk.Label(header_f, text="対象カメラ", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=15, anchor="w").pack(side=tk.LEFT, padx=5)
                tk.Label(header_f, text="PatchCoreモデルファイル (.ckpt)", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=32, anchor="w").pack(side=tk.LEFT, padx=5)
                tk.Label(header_f, text="判定しきい値", font=FONT_BOLD, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=12, anchor="w").pack(side=tk.LEFT, padx=5)

                for c in self.temp_data["cameras"]:
                    c_id = str(c["id"])
                    c_cond = st[cond_key].setdefault(c_id, {"model_path": "", "threshold": 0.49})
                    if not isinstance(c_cond, dict):
                        c_cond = {"model_path": "", "threshold": 0.49}
                        st[cond_key][c_id] = c_cond
                        
                    row_f = tk.Frame(target_frame, bg=COLOR_BG_PANEL)
                    row_f.pack(fill=tk.X, pady=4)
                    
                    # 対象カメラ名
                    tk.Label(row_f, text=c["name"], font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=15, anchor="w").pack(side=tk.LEFT, padx=5)
                    
                    # ckpt モデルファイル指定
                    v_path = tk.StringVar(value=c_cond.get("model_path", ""))
                    e_path = tk.Entry(row_f, textvariable=v_path, font=FONT_SET_VAL, width=32, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, insertbackground="white", relief="flat")
                    e_path.pack(side=tk.LEFT, padx=5)
                    Tooltip(e_path, "PatchCoreモデルファイル (.ckpt) のパスを入力または参照ボタンで選択します (未指定時はSKIP判定)")
                    
                    # 参照ボタン
                    def _pick_model(var=v_path):
                        p = filedialog.askopenfilename(
                            title="PatchCoreモデルファイルを選択",
                            parent=self,
                            filetypes=[("Anomalib checkpoint", "*.ckpt"), ("すべてのファイル", "*.*")]
                        )
                        if p:
                            var.set(p)
                            self._mark_changed()
                    
                    btn_browse = tk.Button(row_f, text="参照", command=_pick_model, font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, relief="flat", padx=6, takefocus=False)
                    btn_browse.pack(side=tk.LEFT, padx=2)
                    Tooltip(btn_browse, "PatchCoreモデルファイル (.ckpt) を参照して選択します")
                    
                    # 個別しきい値スライダー
                    v_thr = tk.StringVar(value=str(c_cond.get("threshold", 0.49)))
                    sp_thr = self._spinbox(row_f, v_thr, 0.0, 1.0, 0.01, width=8)
                    sp_thr.pack(side=tk.LEFT, padx=10)
                    Tooltip(sp_thr, "異常判定しきい値を設定します (アノマリスコアがこの値以上でNG判定)")
                    
                    def _make_updater(cid=c_id, vp=v_path, vt=v_thr, cond_dict=c_cond):
                        def _update(*args):
                            try:
                                cond_dict["model_path"] = vp.get().strip()
                                cond_dict["threshold"] = float(vt.get().strip())
                                self._mark_changed()
                            except Exception:
                                pass
                        return _update
                        
                    updater = _make_updater()
                    v_path.trace_add("write", updater)
                    v_thr.trace_add("write", updater)
                    
                    # PatchCoreテスト用個別ライブテストボタン
                    def _test_pc(cam_obj=c, vp=v_path, vt=v_thr):
                        model_path = vp.get().strip()
                        try:
                            threshold = float(vt.get().strip())
                        except ValueError:
                            threshold = 0.49
                            
                        if not model_path or not os.path.exists(model_path):
                            messagebox.showerror("エラー", "モデルファイル (.ckpt) が選択されていないか、ファイルが存在しません。", parent=self)
                            return
                            
                        self.test_patchcore_live(cam_obj, model_path, threshold)
                        
                    btn_test = tk.Button(row_f, text="テスト", font=FONT_BTN_LARGE, bg=COLOR_ACCENT, fg="black", relief="flat", takefocus=False)
                    btn_test.pack(side=tk.LEFT, padx=10)
                    btn_test.config(command=_test_pc)
                    Tooltip(btn_test, "現在のカメラ映像に対し選択されたPatchCoreモデルでリアルタイム判定テストを行います")

            def _update_canvas_scroll():
                if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists():
                    self.pat_canvas.configure(scrollregion=self.pat_canvas.bbox("all"))

            cond_container.pack(fill=tk.X, pady=10)

            if is_half_step:
                frame_fr = tk.Frame(cond_container, bg=COLOR_BG_PANEL)
                frame_rr = tk.Frame(cond_container, bg=COLOR_BG_PANEL)

                _build_table(frame_fr, "fr")
                _build_table(frame_rr, "rr")

                # 初期表示は Fr
                frame_fr.pack(fill=tk.X)

                part_frm = tk.Frame(head_f, bg=COLOR_BG_PANEL)
                part_frm.pack(side=tk.RIGHT)

                btn_fr = tk.Button(part_frm, text="Fr 判定条件", font=FONT_BOLD, width=11, relief="flat", takefocus=False)
                btn_rr = tk.Button(part_frm, text="Rr 判定条件", font=FONT_BOLD, width=11, relief="flat", takefocus=False)

                def _show_fr():
                    frame_rr.pack_forget()
                    frame_fr.pack(fill=tk.X)
                    btn_fr.config(bg=COLOR_ACCENT, fg="black")
                    btn_rr.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)
                    _update_canvas_scroll()

                def _show_rr():
                    frame_fr.pack_forget()
                    frame_rr.pack(fill=tk.X)
                    btn_rr.config(bg=COLOR_ACCENT, fg="black")
                    btn_fr.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)
                    _update_canvas_scroll()

                btn_fr.config(command=_show_fr)
                btn_rr.config(command=_show_rr)
                btn_fr.pack(side=tk.LEFT, padx=(0, 4))
                btn_rr.pack(side=tk.LEFT)
                btn_fr.config(bg=COLOR_ACCENT, fg="black")
                btn_rr.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN)
            else:
                frame_single = tk.Frame(cond_container, bg=COLOR_BG_PANEL)
                _build_table(frame_single, "single")
                frame_single.pack(fill=tk.X)

        for t in self.temp_data["gpio"]["triggers"]:
            _create_trigger_card(t)

        self.after(50, lambda: self.pat_canvas.configure(scrollregion=self.pat_canvas.bbox("all")) if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists() else None)
        self.after(60, lambda: self.pat_canvas.yview_moveto(y_pos) if hasattr(self, "pat_canvas") and self.pat_canvas.winfo_exists() else None)

    def test_patchcore_live(self, camera_obj, model_path, threshold):
        """設定画面で、指定されたカメラとPatchCoreモデルを使用したリアルタイム判定ライブテストを実行します"""
        c_idx = int(camera_obj.get("index", 0))
        test_win = tk.Toplevel(self)
        test_win.title(f"PatchCore ライブテスト (インデックス: {c_idx})")
        test_win.geometry("640x540")
        test_win.transient(self)
        test_win.grab_set()
        
        lbl_info = tk.Label(test_win, text="モデル読み込み中...", font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        lbl_info.pack(fill=tk.X, pady=5)
        
        lbl_img = tk.Label(test_win, bg="black")
        lbl_img.pack(fill=tk.BOTH, expand=True)
        
        app = getattr(self.master, "app_instance", None)
        model_pc = None
        if app:
            model_pc = app.get_patchcore_model(model_path)
            
        if model_pc is None:
            try:
                from anomalib.models import Patchcore
                device = "cuda" if torch.cuda.is_available() else "cpu"
                model_pc = Patchcore.load_from_checkpoint(model_path).to(device)
                model_pc.eval()
            except Exception as e:
                messagebox.showerror("エラー", f"モデルの読み込みに失敗しました:\n{e}", parent=self)
                test_win.destroy()
                return
        
        cap = cv2.VideoCapture(c_idx)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            messagebox.showerror("エラー", f"カメラ {c_idx} を開けませんでした。", parent=self)
            test_win.destroy()
            return

        def update_frame():
            if not test_win.winfo_exists():
                cap.release()
                return
            ret, frame = cap.read()
            if ret:
                try:
                    if app:
                        score, amap = app.predict_patchcore(model_pc, frame)
                        overlay = app.generate_heatmap_overlay(frame, amap)
                    else:
                        score, amap = self._predict_patchcore_fallback(model_pc, frame)
                        overlay = self._generate_heatmap_fallback(frame, amap)
                        
                    is_abnormal = score >= threshold
                    res_str = "異常 (Abnormal)" if is_abnormal else "正常 (Normal)"
                    res_color = COLOR_NG if is_abnormal else COLOR_OK
                    
                    lbl_info.config(text=f"判定: {res_str}　スコア: {score:.4f} (しきい値: {threshold:.2f})", fg=res_color)
                    
                    overlay_rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(overlay_rgb)
                    img = img.resize((640, 440))
                    photo = ImageTk.PhotoImage(image=img)
                    lbl_img.config(image=photo)
                    lbl_img.image = photo
                except Exception as e:
                    lbl_info.config(text=f"判定エラー: {e}", fg=COLOR_NG)
            else:
                lbl_img.config(text="フレームを取得できません", fg="white")
            test_win.after(30, update_frame)

        update_frame()

    def _predict_patchcore_fallback(self, model, cv_img):
        """アプリ本体にアクセスできない場合の推論用フォールバックメソッド"""
        try:
            rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            device = next(model.parameters()).device
            from torchvision.transforms import functional as F
            resized = F.resize(pil_img, (256, 256))
            tensor = F.to_tensor(resized)
            tensor = F.normalize(tensor, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            tensor = tensor.unsqueeze(0).to(device)
            
            model.eval()
            with torch.no_grad():
                outputs = model(tensor)
            
            anomaly_score = 0.0
            anomaly_map = None
            if isinstance(outputs, dict):
                anomaly_score = outputs.get("pred_score") or outputs.get("anomaly_score") or 0.0
                anomaly_map = outputs.get("anomaly_map")
            elif hasattr(outputs, "pred_score"):
                anomaly_score = outputs.pred_score
                anomaly_map = getattr(outputs, "anomaly_map", None)
            
            if isinstance(anomaly_score, torch.Tensor):
                anomaly_score = anomaly_score.item()
            return float(anomaly_score), anomaly_map
        except Exception:
            return 0.0, None

    def _generate_heatmap_fallback(self, cv_img, anomaly_map):
        """アプリ本体にアクセスできない場合のヒートマップ生成用フォールバックメソッド"""
        if anomaly_map is None:
            return cv_img.copy()
        try:
            if isinstance(anomaly_map, torch.Tensor):
                anomaly_map = anomaly_map.detach().cpu().numpy()
            if anomaly_map.ndim == 4:
                anomaly_map = anomaly_map[0, 0]
            elif anomaly_map.ndim == 3:
                anomaly_map = anomaly_map[0]
            amin, amax = anomaly_map.min(), anomaly_map.max()
            if amax - amin > 1e-5:
                norm_map = (anomaly_map - amin) / (amax - amin)
            else:
                norm_map = anomaly_map
            norm_map = (norm_map * 255).astype(np.uint8)
            h, w = cv_img.shape[:2]
            heatmap = cv2.resize(norm_map, (w, h), interpolation=cv2.INTER_LINEAR)
            heatmap_color = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
            return cv2.addWeighted(cv_img, 0.6, heatmap_color, 0.4, 0)
        except Exception:
            return cv_img.copy()


    # ---- 画素数タブ ----
    def setup_res(self):
        RES_MAP = {
            "320x240": "320x240 (QVGA)",
            "640x480": "640x480 (VGA)",
            "1280x720": "1280x720 (HD)",
            "1920x1080": "1920x1080 (Full HD)",
            "3840x2160": "3840x2160 (4K)"
        }

        def _to_friendly(s): return RES_MAP.get(s, s)
        def _to_raw(s): return s.split(" ")[0] if "x" in s else s

        main_f = self.create_scrollable_panel(self.t_res)

        def _make_group(title):
            outer, inner = create_card(main_f, title)
            outer.pack(fill=tk.X, padx=20, pady=(10, 15))
            return inner

        def _row(parent, label, key, options, tip):
            row_f = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row_f.pack(fill=tk.X, pady=6, padx=10)
            
            lbl = tk.Label(row_f, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                           fg=COLOR_TEXT_MAIN, anchor="w", width=30)
            lbl.pack(side=tk.LEFT)
            Tooltip(lbl, tip)
            
            raw_val = self.temp_data["storage"].get(key, options[0])
            v = tk.StringVar(value=_to_friendly(raw_val))
            
            friendly_opts = [_to_friendly(o) for o in options]
            cb = ttk.Combobox(row_f, textvariable=v, values=friendly_opts,
                              font=FONT_SET_VAL, state="readonly", width=25)
            cb.pack(side=tk.RIGHT, padx=5)
            
            def _on_change(*a, k=key, var=v, widget=cb):
                if not self.winfo_exists(): return
                raw = _to_raw(var.get())
                self.temp_data["storage"][k] = raw
                self._mark_changed()
                if k == "capture_res":
                    _update_all_filters()

            v.trace_add("write", _on_change)
            return cb, v, options

        inner_a = _make_group("基本撮影設定")
        cb_cap, v_cap, opt_cap = _row(inner_a, "撮影解像度", "capture_res", RES_OPTIONS, "カメラ映像の縦横解像度。")

        inner_b = _make_group("表示設定")
        _row(inner_b, "プレビュー解像度", "preview_res", RES_OPTIONS_PREVIEW, "プレビュー画面の縮小サイズ。")

        inner_c = _make_group("保存設定")
        
        tk.Label(inner_c, text="▼ 検査モードの保存画素数", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT).pack(anchor="w", padx=10, pady=(5, 0))
        cb_ok, v_ok, opt_ok = _row(inner_c, "OK(正常)保存画像", "res_ok", RES_OPTIONS_SAVE, "判定OK時に保存するヒートマップ画像サイズ。")
        cb_ng, v_ng, opt_ng = _row(inner_c, "NG(異常)保存画像", "res_ng", RES_OPTIONS_SAVE, "判定NG時に保存するヒートマップ画像サイズ。")
        cb_skip, v_skip, opt_skip = _row(inner_c, "スキップ時保存画像", "res_skip", RES_OPTIONS_SAVE, "検査SKIP時に保存する画像サイズ。")

        tk.Frame(inner_c, bg=COLOR_BG_MAIN, height=1).pack(fill=tk.X, padx=10, pady=10)

        tk.Label(inner_c, text="▼ 撮影モードの保存画素数", font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT).pack(anchor="w", padx=10, pady=(0, 0))
        cb_rec, v_rec, opt_rec = _row(inner_c, "判定対象画像", "res_record", RES_OPTIONS_SAVE, "撮影モード時の保存画像サイズ。")
        _row(inner_c, "判定対象外画像", "res_record_skip", RES_OPTIONS_SAVE, "SKIP時の保存画像サイズ。")

        def _get_area(res_str):
            if "x" not in res_str: return 0
            try:
                w, h = map(int, res_str.split("x"))
                return w * h
            except: return 0

        def _update_all_filters():
            cap_val = _to_raw(v_cap.get())
            cap_area = _get_area(cap_val)
            targets = [ 
                (cb_ok, opt_ok, "res_ok"), 
                (cb_ng, opt_ng, "res_ng"), 
                (cb_skip, opt_skip, "res_skip"),
                (cb_rec, opt_rec, "res_record")
            ]
            for cb, opts, k in targets:
                new_opts = [o for o in opts if ("x" not in str(o)) or _get_area(o) <= cap_area]
                cb.config(values=[_to_friendly(o) for o in new_opts])
                raw_curr = _to_raw(cb.get())
                if raw_curr not in new_opts:
                    fallback = new_opts[0] if "x" not in new_opts[0] else cap_val
                    cb.set(_to_friendly(fallback))
                    self.temp_data["storage"][k] = fallback

        self.after(200, _update_all_filters)

    # ---- システム設定タブ ----
    def setup_sys(self):
        scroll_f = self.create_scrollable_panel(self.t_sys)
        s = self.temp_data["inference"]

        def _make_group(parent, title, pady=(10, 4)):
            outer, inner = create_card(parent, title)
            outer.pack(fill=tk.X, padx=20, pady=pady)
            return inner

        def _row_frame(parent, column_widths=(280, 1)):
            f = tk.Frame(parent, bg=COLOR_BG_PANEL)
            f.pack(fill=tk.X, pady=4)
            f.columnconfigure(0, minsize=column_widths[0])
            return f

        def _lbl(parent, text, tip=""):
            l = tk.Label(parent, text=text, font=FONT_SET_VAL,
                         bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=22)
            l.pack(side=tk.LEFT, padx=(0, 8))
            if tip:
                Tooltip(l, tip)
            return l

        def _unit(parent, text):
            lbl = tk.Label(parent, text=text, font=FONT_SET_VAL,
                           bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
            lbl.pack(side=tk.LEFT, padx=(2, 0))
            return lbl

        def _entry_w(parent, var, width=10):
            e = self._entry(parent, var, width=width)
            e.pack(side=tk.LEFT)
            return e

        def _browse_btn(parent, var, mode="file", filetypes=None):
            def _pick():
                if mode == "dir":
                    p = filedialog.askdirectory(title="フォルダを選択", parent=self)
                else:
                    p = filedialog.askopenfilename(
                        title="ファイルを選択",
                        parent=self,
                        filetypes=filetypes or [("すべてのファイル", "*.*")])
                if p:
                    var.set(p)
            btn = tk.Button(parent, text="参照", font=FONT_NORMAL,
                            bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                            relief="flat", padx=6, pady=2, cursor="hand2",
                            command=_pick)
            btn.pack(side=tk.LEFT, padx=(6, 0))
            Tooltip(btn, "フォルダ/ファイルをエクスプローラーから選択します。")
            return btn

        def _play_btn(parent, var):
            def _play():
                try:
                    import pygame
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()
                    p = var.get().strip()
                    if p and os.path.exists(p):
                        pygame.mixer.music.load(p)
                        pygame.mixer.music.play(0)
                    else:
                        messagebox.showwarning("テスト再生", "ファイルが見つかりません:\n" + p, parent=self)
                except Exception as ex:
                    messagebox.showwarning("テスト再生エラー", str(ex), parent=self)
            btn = tk.Button(parent, text="テスト再生", font=FONT_NORMAL,
                            bg="#37474f", fg=COLOR_TEXT_MAIN,
                            relief="flat", padx=6, pady=2, cursor="hand2",
                            command=_play)
            btn.pack(side=tk.LEFT, padx=(4, 0))
            return btn

        # グループ1: パラメータ設定
        g1 = _make_group(scroll_f, "基本パラメータ設定", pady=(16, 4))
        num_params = [
            ("最大リトライ回数:", "max_retries", "回", "リトライ撮影の上限回数です。", 0, 99, 1),
            ("撮影間隔:", "burst_interval", "sec", "バースト撮影の一時待機秒数です。", 0.0, 10.0, 0.1),
            ("結果表示時間:", "result_display_time", "sec", "判定結果の静止表示時間です。", 0.0, 60.0, 0.5),
            ("プレビュー更新レート:", "preview_fps", "fps", "プレビュー映像描画の更新FPS値。", 0.1, 60.0, 0.1),
        ]
        for lbl_txt, key, unit, tip, min_val, max_val, inc in num_params:
            r = _row_frame(g1)
            _lbl(r, lbl_txt, tip)
            v = tk.StringVar(value=str(s.get(key, "")))
            ent = self._spinbox(r, v, min_val, max_val, inc, width=8, key_path=f"inference.{key}")
            ent.pack(side=tk.LEFT)
            _unit(r, unit)
            def _mk_upd(ky=key, var=v, e=ent):
                def _upd(*a):
                    val = var.get()
                    try:
                        s[ky] = float(val) if "." in val else int(val)
                    except Exception: pass
                    self._mark_changed()
                return _upd
            v.trace_add("write", _mk_upd())

        # グループ2: 出力制御
        g2 = _make_group(scroll_f, "出力制御")

        r_ok = _row_frame(g2)
        _lbl(r_ok, "OK出力時間:", "OK判定時、ONを出力し続ける時間です。")
        v_ok_t = tk.StringVar(value=str(s.get("ok_output_time", "0.5")))
        ok_sp = self._spinbox(r_ok, v_ok_t, 0.0, 60.0, 0.1, width=8)
        ok_sp.pack(side=tk.LEFT)
        _unit(r_ok, "sec")
        def _upd_ok_t(*a):
            try: s["ok_output_time"] = float(v_ok_t.get())
            except Exception: pass
        v_ok_t.trace_add("write", _upd_ok_t)

        r_ng = _row_frame(g2)
        _lbl(r_ng, "NG出力時間:", "NG判定時の出力時間。空欄にするとブザー停止が押されるまで保持します。")
        v_ng_t = tk.StringVar(value=str(s.get("ng_output_time", "")))
        ng_sp = self._spinbox(r_ng, v_ng_t, 0.0, 60.0, 0.1, width=8)
        ng_sp.pack(side=tk.LEFT)
        _unit(r_ng, "秒（空欄時は停止ボタンまで保持）")
        def _upd_ng_t(*a):
            val = v_ng_t.get().strip()
            try: s["ng_output_time"] = float(val) if val else ""
            except Exception: pass
        v_ng_t.trace_add("write", _upd_ng_t)

        r_hold = _row_frame(g2)
        v_hold = tk.BooleanVar(value=bool(s.get("ng_output_hold", False)))
        cb_ng_hold = tk.Checkbutton(
            r_hold, text="ブザー停止ボタンが押されるまでNG出力を保持する",
            variable=v_hold, font=FONT_NORMAL,
            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT
        )
        cb_ng_hold.pack(side=tk.LEFT)
        Tooltip(cb_ng_hold, "ブザー停止ボタンが押されるまでNG出力を保持します。")

        def _upd_hold(*a):
            val = v_hold.get()
            s["ng_output_hold"] = val
            if val:
                ng_sp.config(state="disabled")
            else:
                ng_sp.config(state="normal")
            self._mark_changed()
        v_hold.trace_add("write", _upd_hold)
        if s.get("ng_output_hold", False):
            ng_sp.config(state="disabled")

        # グループ3: ファイルパス
        g3 = _make_group(scroll_f, "ファイルパス設定")

        r_res = _row_frame(g3)
        _lbl(r_res, "結果出力先フォルダ:", "ログ、CSV、画像の出力先ディレクトリ。")
        vp = tk.StringVar(value=self.temp_data["storage"].get("results_dir", ""))
        _entry_w(r_res, vp, width=40)
        _browse_btn(r_res, vp, mode="dir")
        vp.trace_add("write", lambda *a: self.temp_data["storage"].update({"results_dir": vp.get()}))

        # グループ4: 音声設定
        g4 = _make_group(scroll_f, "音声設定")

        r_bng = _row_frame(g4)
        _lbl(r_bng, "NG時ブザー音:", "不合格（異常検出）時に鳴らす音声パス。")
        vb = tk.StringVar(value=s.get("buzzer_path", ""))
        _entry_w(r_bng, vb, width=35)
        _browse_btn(r_bng, vb, mode="file", filetypes=[("音声ファイル", "*.mp3 *.wav *.ogg"), ("すべて", "*.*")])
        _play_btn(r_bng, vb)
        vb.trace_add("write", lambda *a: s.update({"buzzer_path": vb.get()}))

        r_bok = _row_frame(g4)
        _lbl(r_bok, "OK時ブザー音:", "合格（正常判定）時に鳴らす音声パス。")
        vob = tk.StringVar(value=s.get("ok_buzzer_path", ""))
        _entry_w(r_bok, vob, width=35)
        _browse_btn(r_bok, vob, mode="file", filetypes=[("音声ファイル", "*.mp3 *.wav *.ogg"), ("すべて", "*.*")])
        _play_btn(r_bok, vob)
        vob.trace_add("write", lambda *a: s.update({"ok_buzzer_path": vob.get()}))

        # グループ5: 自動削除
        g5 = _make_group(scroll_f, "容量監視 / 自動削除")
        st = self.temp_data["storage"]

        r_ad = _row_frame(g5)
        v_ad = tk.BooleanVar(value=bool(st.get("auto_delete_enabled", False)))
        cb = tk.Checkbutton(
            r_ad, text="古い結果画像を自動削除する",
            variable=v_ad, onvalue=True, offvalue=False,
            font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, relief="flat"
        )
        cb.pack(side=tk.LEFT)
        v_ad.trace_add("write", lambda *a: st.update({"auto_delete_enabled": v_ad.get()}))

        r_mg = _row_frame(g5)
        _lbl(r_mg, "最大容量上限:", "ディスク内の自動削除の閾値。")
        v_mg = tk.StringVar(value=str(st.get("max_results_gb", "")))
        mg_sp = self._spinbox(r_mg, v_mg, 0.1, 9999.0, 1.0, width=8)
        mg_sp.pack(side=tk.LEFT)
        _unit(r_mg, "GB")
        def _upd_mg(*a):
            try: st["max_results_gb"] = float(v_mg.get())
            except Exception: pass
        v_mg.trace_add("write", _upd_mg)

        v_used = tk.StringVar(value="現在の使用量: 計算中...")
        lbl_used = tk.Label(g5, textvariable=v_used, font=FONT_SET_VAL,
                            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w")
        lbl_used.pack(fill=tk.X, pady=(4, 0))

        def _calc_storage():
            import shutil as _shutil
            _res_dir = st.get("results_dir", "")
            try:
                if _res_dir and os.path.exists(_res_dir):
                    _used = sum(f.stat().st_size for f in Path(_res_dir).rglob('*') if f.is_file())
                    _used_gb = _used / (1024**3)
                    _total_gb = _shutil.disk_usage(_res_dir).total / (1024**3)
                    msg = f"現在の使用量: {_used_gb:.2f} GB / ディスク合計: {_total_gb:.1f} GB"
                    self.after(0, lambda: v_used.set(msg))
                else:
                    self.after(0, lambda: v_used.set("現在の使用量: -"))
            except Exception:
                self.after(0, lambda: v_used.set("(使用量の取得に失敗しました)"))

        threading.Thread(target=_calc_storage, daemon=True).start()

        # グループ6: 生産ライン同期設定
        g6 = _make_group(scroll_f, "生産ライン同期設定")
        st_sys = self.temp_data.setdefault("system", {})

        r_step = _row_frame(g6)
        v_step = tk.BooleanVar(value=bool(st_sys.get("commit_half_step", False)))
        cb_step = tk.Checkbutton(
            r_step, text="ドアライン対応 (Fr/Rr 分割判定 & 0.5刻みコミット)",
            variable=v_step, onvalue=True, offvalue=False,
            font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, relief="flat"
        )
        cb_step.pack(side=tk.LEFT)
        Tooltip(cb_step, "チェックを入れると、コミット番号が 0.5 刻み (Fr/Rr表記) で進み、パターン判定条件を Fr と Rr で個別に設定できます。")
        
        def _format_delay_value(val, half_step):
            try: num = float(val)
            except (TypeError, ValueError): return "0"
            if half_step:
                if abs(num - round(num)) < 1e-9:
                    return str(int(round(num)))
                return f"{num:.1f}"
            return str(int(num))

        def _apply_delay_spinbox_mode():
            half = v_step.get()
            if half:
                delay_sp.config(from_=0.0, to=99.0, increment=0.5)
                delay_unit_lbl.config(text="サイクル（0.5刻みで設定可能、0で遅延なし）")
            else:
                delay_sp.config(from_=0, to=99, increment=1)
                delay_unit_lbl.config(text="サイクル（整数のみ、0で遅延なし）")

        def _upd_step(*a):
            st_sys["commit_half_step"] = v_step.get()
            _apply_delay_spinbox_mode()
            if not v_step.get():
                try:
                    num = float(v_delay.get())
                    if abs(num - int(num)) > 1e-9:
                        v_delay.set(str(int(num)))
                except (TypeError, ValueError): pass
            self._mark_changed()
        v_step.trace_add("write", _upd_step)

        r_delay = _row_frame(g6)
        _lbl(r_delay, "仕様情報遅延サイクル数:", "トリガー時に取得した仕様情報を、何サイクル（コミット数）後に実際の検査に適用するか。")
        half_init = bool(st_sys.get("commit_half_step", False))
        v_delay = tk.StringVar(value=_format_delay_value(st_sys.get("delay_cycles", 0), half_init))
        self._last_valid_delay_str = v_delay.get()
        self._delay_revert_guard = False
        delay_sp = self._spinbox(r_delay, v_delay, 0.0, 99.0, 0.5, width=8)
        delay_sp.pack(side=tk.LEFT)
        delay_unit_lbl = _unit(r_delay, "サイクル（0.5刻みで設定可能、0で遅延なし）")
        _apply_delay_spinbox_mode()

        def _upd_delay(*a):
            if self._delay_revert_guard: return
            val = v_delay.get().strip()
            if val in ("", "-", ".", "-."): return
            try: num = float(val)
            except ValueError: return
            if not v_step.get():
                if abs(num - int(num)) > 1e-9:
                    self._delay_revert_guard = True
                    v_delay.set(self._last_valid_delay_str)
                    self._delay_revert_guard = False
                    messagebox.showerror("入力エラー", "0.5刻みモードがOFFのとき、遅延サイクル数は整数のみ指定できます。", parent=self)
                    return
                st_sys["delay_cycles"] = int(num)
                self._last_valid_delay_str = str(int(num))
            else:
                st_sys["delay_cycles"] = num
                self._last_valid_delay_str = _format_delay_value(num, True)
            self._mark_changed()

        v_delay.trace_add("write", _upd_delay)

        # =====================================================================
        # 5. システムツール & メンテナンス
        # =====================================================================
        g5 = _make_group(scroll_f, "システムツール & メンテナンス", pady=(10, 16))

        # 起動ショートカット作成
        r_sh = _row_frame(g5)
        _lbl(r_sh, "起動スクリプト生成:", "デスクトップにワンクリックで本アプリを起動するファイルを作成します。")

        def _get_desktop_path():
            home = os.path.expanduser("~")
            if sys.platform.startswith("win"):
                desktop = os.path.join(home, "Desktop")
                if os.path.exists(desktop): return desktop
                try:
                    import winreg
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
                    path, _ = winreg.QueryValueEx(key, "Desktop")
                    winreg.CloseKey(key)
                    expanded = os.path.expandvars(path)
                    if os.path.exists(expanded): return expanded
                except Exception: pass
                return desktop
            else:
                desktop = os.path.join(home, "Desktop")
                if os.path.exists(desktop): return desktop
                desktop_ja = os.path.join(home, "デスクトップ")
                if os.path.exists(desktop_ja): return desktop_ja
                return desktop

        def _create_desktop_launcher():
            try:
                desktop_dir = _get_desktop_path()
                if not os.path.exists(desktop_dir):
                    os.makedirs(desktop_dir, exist_ok=True)

                app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                python_exe = sys.executable

                is_win = sys.platform.startswith("win")
                if is_win:
                    filename = "AI自動検査システム起動.bat"
                    file_path = os.path.join(desktop_dir, filename)
                    content = (
                        "@echo off\n"
                        "chcp 65001 > nul\n"
                        "title AI自動検査システム (PatchCore/PaDiM)\n"
                        f'cd /d "{app_dir}"\n'
                        f'"{python_exe}" main.py\n'
                        "if %errorlevel% neq 0 (\n"
                        "    echo.\n"
                        "    echo エラーが発生しました。キーを押すと終了します...\n"
                        "    pause > nul\n"
                        ")\n"
                    )
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                else:
                    filename = "start_inspection_app.sh"
                    file_path = os.path.join(desktop_dir, filename)
                    content = (
                        "#!/bin/bash\n"
                        f'cd "{app_dir}"\n'
                        f'"{python_exe}" main.py\n'
                        "if [ $? -ne 0 ]; then\n"
                        '    read -p "エラーが発生しました。Enterキーを押すと終了します..."\n'
                        "fi\n"
                    )
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    try:
                        os.chmod(file_path, 0o755)
                    except Exception:
                        pass

                messagebox.showinfo("ショートカット作成完了", f"デスクトップに起動スクリプトを作成しました:\n\n{file_path}", parent=self)
            except Exception as ex:
                messagebox.showerror("作成失敗", f"起動スクリプトの作成中にエラーが発生しました:\n{ex}", parent=self)

        is_win = sys.platform.startswith("win")
        btn_text = "デスクトップに起動ファイルを作成 (.bat)" if is_win else "デスクトップに起動ファイルを作成 (.sh)"

        btn_shortcut = tk.Button(
            r_sh, text=btn_text, font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=_create_desktop_launcher
        )
        btn_shortcut.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(btn_shortcut, f"デスクトップに本アプリを起動する{'batch (.bat)' if is_win else 'shell (.sh)'}ファイルを作成します")

        # 日時設定
        r_datetime = _row_frame(g5)
        _lbl(r_datetime, "ラズパイ本体日時設定:", "本体のシステム日付・時刻を手動設定または端末同期します。")

        def _open_datetime_dialog():
            SystemDateTimeDialog(self)

        btn_dt = tk.Button(
            r_datetime, text="ラズパイ本体の日時を設定", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=_open_datetime_dialog
        )
        btn_dt.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(btn_dt, "Linux/Raspberry Piのシステム日時(timedatectl/date)を設定するダイアログを開きます")

        # USBスピーカー設定
        r_audio = _row_frame(g5)
        _lbl(r_audio, "USBスピーカー自動設定:", "音が出ない場合に、ALSA/PulseAudio/PipeWire等の出力先とミュートを自動解除します。")

        def _fix_usb_audio():
            if sys.platform.startswith("win"):
                messagebox.showinfo(
                    "USBスピーカー設定 (Windows)",
                    "Windows環境のためLinuxオーディオ設定 (ALSA/PulseAudio/PipeWire/asoundrc) はスキップされました。\n"
                    "※ラズパイ/Linux環境で自動出力設定が実行されます。",
                    parent=self
                )
                return

            import subprocess
            logs = []

            # 1. ALSA mixer (amixer) ミュート解除・ボリューム100%化
            channels = ["Master", "PCM", "Speaker", "Headphone", "Line"]
            for card_idx in range(5):
                for ch in channels:
                    cmd = ["amixer", "-c", str(card_idx), "sset", ch, "100%", "unmute"]
                    try:
                        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                        if r.returncode == 0: logs.append(f"ALSA: card {card_idx} {ch} -> 100% unmute")
                    except Exception: pass

            for ch in channels:
                try: subprocess.run(["amixer", "sset", ch, "100%", "unmute"], capture_output=True, text=True, timeout=3)
                except Exception: pass

            try:
                subprocess.run(["sudo", "alsactl", "store"], capture_output=True, text=True, timeout=3)
                logs.append("ALSA: alsactl store 実行完了")
            except Exception: pass

            # 2. PulseAudio / PipeWire (pactl / wpctl)
            try:
                subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], capture_output=True, text=True, timeout=3)
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"], capture_output=True, text=True, timeout=3)
                logs.append("PulseAudio: @DEFAULT_SINK@ -> unmute & 100%")
            except Exception: pass

            # 3. Pygame Mixer 再初期化
            try:
                import pygame
                pygame.mixer.quit()
                pygame.mixer.init()
                logs.append("Pygame: Audio Mixer 再初期化完了")
            except Exception: pass

            msg = "USBスピーカー自動出力設定を実行しました。\n\n【詳細ログ】\n" + ("\n".join(logs) if logs else "設定を実行しました。")
            messagebox.showinfo("USBスピーカー設定完了", msg, parent=self)

        btn_audio = tk.Button(
            r_audio, text="USBスピーカー音声出力を自動復旧・設定", font=FONT_NORMAL,
            bg=COLOR_ACCENT, fg="white",
            relief="flat", padx=10, pady=4, cursor="hand2",
            command=_fix_usb_audio
        )
        btn_audio.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(btn_audio, "音が出ない場合に、USBスピーカーを優先出力先に変更しミュート解除・音量最大化を行います")

    # ---- 保存 / GPIO テスト ----

    def _sync_pattern_conditions(self):
        """設定データ内のパターン設定の構造をPatchCore用に調整・移行する"""
        active_pids = set(self.temp_data.get("pattern_order", []))
        all_pids = list(self.temp_data.get("patterns", {}).keys())
        for pid in all_pids:
            if pid not in active_pids:
                self.temp_data["patterns"].pop(pid, None)

        for pid in self.temp_data.get("pattern_order", []):
            p = self.temp_data["patterns"].get(pid)
            if not p:
                continue
            
            if "stages" not in p:
                p["stages"] = {}
                
            for tid, stage in p["stages"].items():
                for ck in ("conditions", "conditions_fr", "conditions_rr"):
                    if ck in stage:
                        if not isinstance(stage[ck], dict):
                            stage[ck] = {}
                        for cam in self.temp_data["cameras"]:
                            c_id = str(cam["id"])
                            c_cond = stage[ck].get(c_id)
                            if not isinstance(c_cond, dict):
                                stage[ck][c_id] = {"model_path": "", "threshold": 0.49}
                    elif ck == "conditions":
                        stage["conditions"] = {}
                        for cam in self.temp_data["cameras"]:
                            c_id = str(cam["id"])
                            stage["conditions"][c_id] = {"model_path": "", "threshold": 0.49}

    def validate_pins(self):
        used_pins = {}
        def _get_val(v):
            if v is None: return -1
            s_val = str(v).strip()
            if not s_val: return -1
            try: return int(s_val)
            except: return -1

        all_pins = []  

        for t in self.temp_data["gpio"]["triggers"]:
            all_pins.append((_get_val(t["pin"]), t["name"]))
        for s in self.temp_data["gpio"].get("pattern_pins", []):
            all_pins.append((_get_val(s["pin"]), s["name"]))
        outputs = self.temp_data["gpio"]["outputs"]
        all_pins.append((_get_val(outputs["ok"]), "OK出力"))
        all_pins.append((_get_val(outputs["ng"]), "NG出力"))

        for p, name in all_pins:
            if p == -1:
                messagebox.showerror("バリデーションエラー", f"「{name}」のピン番号が未入力または無効です", parent=self)
                return False
            if p not in VALID_BCM_PINS:
                messagebox.showerror("バリデーションエラー", f"「{name}」のピン番号 {p} は有効なBCMピンではありません\n有効なピン: {sorted(VALID_BCM_PINS)}", parent=self)
                return False
            if p in used_pins:
                messagebox.showerror("バリデーションエラー", f"ピン {p} が重複しています: {name} と {used_pins[p]}", parent=self)
                return False
            used_pins[p] = name
        
        return True

    def _validate_delay_cycles(self):
        st_sys = self.temp_data.get("system", {})
        try:
            delay = float(st_sys.get("delay_cycles", 0))
        except (TypeError, ValueError):
            messagebox.showerror("バリデーションエラー", "遅延サイクル数に有効な数値を入力してください。", parent=self)
            return False
        if not bool(st_sys.get("commit_half_step", False)):
            if abs(delay - int(delay)) > 1e-9:
                messagebox.showerror("バリデーションエラー", "0.5刻みモードがOFFのとき、遅延サイクル数は整数のみ指定できます。", parent=self)
                return False
            st_sys["delay_cycles"] = int(delay)
        return True

    def save_and_close(self):
        try:
            self.temp_data["gpio"]["outputs"]["ok"] = self.v_ok.get()
            self.temp_data["gpio"]["outputs"]["ng"] = self.v_ng.get()
        except tk.TclError:
            messagebox.showerror("バリデーションエラー", "出力ピンには数値を入力してください", parent=self)
            return

        for i, c in enumerate(self.temp_data["cameras"]):
            name = c.get("name", "").strip()
            if not name:
                messagebox.showerror("バリデーションエラー", f"カメラ {i+1} の表示名が空です。", parent=self)
                return
            c["name"] = name

        for i, t in enumerate(self.temp_data["gpio"]["triggers"]):
            name = t.get("name", "").strip()
            if not name:
                messagebox.showerror("バリデーションエラー", f"トリガー {i+1} の名称が空です。", parent=self)
                return
            invalid_chars = [char for char in name if char in '<>:"/\\|?*']
            if invalid_chars:
                messagebox.showerror("バリデーションエラー", f"トリガー {i+1} の名称に使用不可の文字が含まれています:\n{', '.join(invalid_chars)}", parent=self)
                return
            t["name"] = name

        for i, s in enumerate(self.temp_data["gpio"].get("pattern_pins", [])):
            name = s.get("name", "").strip()
            if not name:
                messagebox.showerror("バリデーションエラー", f"パターン切替ピン {i+1} の名称が空です。", parent=self)
                return
            s["name"] = name

        for pid, p in self.temp_data["patterns"].items():
            name = p.get("name", "").strip()
            if not name:
                messagebox.showerror("バリデーションエラー", f"パターン 「{pid}」 の名称が空です。", parent=self)
                return
            p["name"] = name

        if "storage" in self.temp_data and "results_dir" in self.temp_data["storage"]:
            self.temp_data["storage"]["results_dir"] = self.temp_data["storage"]["results_dir"].strip()

        if not self.validate_pins():
            return

        if not self._validate_delay_cycles():
            return

        self._sync_pattern_conditions()

        pin_map = {} 
        for pid in self.temp_data["pattern_order"]:
            p = self.temp_data["patterns"][pid]
            cond = tuple(p.get("pin_condition", []))
            if cond not in pin_map:
                pin_map[cond] = []
            pin_map[cond].append(p.get("name", pid))
        
        duplicates = [names for names in pin_map.values() if len(names) > 1]
        if duplicates:
            msg = "以下のパターンで同じ入力ピン条件が設定されています。判定が重複しないよう修正してください:\n\n"
            for names in duplicates:
                msg += f"・{', '.join(names)}\n"
            messagebox.showwarning("バリデーションエラー", msg, parent=self)
            return

        res_dir = self.temp_data["storage"].get("results_dir", "")
        if res_dir:
            try:
                p = Path(res_dir)
                p.mkdir(parents=True, exist_ok=True)
                test_file = p / f".write_test_{int(time.time())}"
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                messagebox.showerror("バリデーションエラー", f"出力先フォルダ「{res_dir}」に書き込み権限がないか、パスが無効です。\nエラー: {e}", parent=self)
                return

        self.settings.data = self.temp_data
        self.settings.save_settings()

        if hasattr(self.master, "app_instance"):
            self.master.app_instance.reset_delay_pattern_queue() # type: ignore

        if hasattr(self, "_live_preview_win") and self._live_preview_win.winfo_exists():
            self._live_preview_win.destroy()
            
        if self.on_close_callback:
            self.on_close_callback()
            
        if hasattr(self.master, "app_instance"):
            app = self.master.app_instance
            app.preview_paused = False # type: ignore

        release_modal_toplevel(self)
        self.destroy()

    def open_gpio_test(self):
        used = set()
        for t in self.temp_data["gpio"]["triggers"]:
            if t["pin"] in used:
                return messagebox.showerror("エラー", f"ピン {t['pin']} が重複しています")
            used.add(t["pin"])
        for s in self.temp_data["gpio"].get("pattern_pins", []):
            if s["pin"] in used:
                return messagebox.showerror("エラー", f"ピン {s['pin']} が重複しています")
            used.add(s["pin"])
        if self.v_ok.get() in used or self.v_ng.get() in used:
            return messagebox.showerror("エラー", "出力ピンが重複しています")

        test_gpio = {
            "triggers": self.temp_data["gpio"]["triggers"],
            "pattern_pins": self.temp_data["gpio"].get("pattern_pins", []),
            "outputs": {"ok": self.v_ok.get(), "ng": self.v_ng.get()}
        }
        if hasattr(self.master, "app_instance"):
            app = self.master.app_instance
            if hasattr(app, 'inputs'):
                for d in app.inputs.values(): # type: ignore
                    d.close()
            if hasattr(app, 'outputs'):
                for d in app.outputs.values(): # type: ignore
                    d.close()
        GPIOTestDialog(self, test_gpio)