#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - メインアプリケーション (InspectionSystem - PatchCore版)
"""

import cv2
import threading
import time
import datetime
import logging
import queue
import os
import sys
import random
import csv
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk
from pathlib import Path
import numpy as np

from .constants import (
    RESULTS_DIR, RESULTS_SUBDIR_NG_RAW, COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_OK, COLOR_NG, COLOR_WARNING,
    FONT_BOLD, FONT_LARGE, FONT_HUGE, FONT_NORMAL, FONT_FAMILY, VERSION,
    DELAYED_SKIP_PATTERN_ID,
)
from .hardware import DigitalInputDevice, OutputDevice, is_gpio_available, MockManager
from .settings import SettingsManager
from .widgets import create_card, Tooltip, HelpWindow, TenKeyDialog, get_commit_display_style
from .dialogs import SettingsDialog

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False


def _ensure_mixer():
    if PYGAME_AVAILABLE and not pygame.mixer.get_init():
        try:
            pygame.mixer.init()
        except Exception:
            pass

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


class InspectionSystem:
    def __init__(self):
        self.settings = SettingsManager()
        self.setup_dirs()
        self.setup_logging()

        self.commit_number = 1
        self.door_latch_pat_id = None  # ドアライン対応時、Fr(整数)時に取得したパターンIDをRr(小数)時にも引き継ぐ
        self.ng_history = []
        self.delay_pattern_queue = []
        self.elapsed_cycles = 0.0
        self.cycle_is_delayed_skip = False
        self.running = True
        self.camera_lock = threading.Lock()
        self.trigger_queue = queue.Queue()
        self.caps = {}
        self.last_frames = {}  
        self.inputs = {}
        self.outputs = {}
        self.out_ok = None
        self.out_ng = None

        self.cycle_active_pat_id = None  
        self.cycle_fired_trigs = set()   
        self.cycle_trig_idx = 0          

        self.result_display_frames = {}   
        self.result_display_until = 0     
        self.preview_paused = False       
        self.inspecting = False           
        self.settings_open = False        

        # PatchCoreモデルキャッシュ用
        self.patchcore_models = {}
        self.model_lock = threading.Lock()
        self.load_model()

        self.setup_hardware()
        self.setup_gui()

        if sys.platform == "win32" and not is_gpio_available():
            self.setup_mock_ui()
        
        self.root.after(500, self.manual_commit_set_initial)
        self.root.after(30 * 1000, self._monitor_storage)

    def setup_mock_ui(self):
        try:
            self.mock_root = tk.Toplevel(self.root)
            self.mock_root.title("仮想GPIOパネル")
            self.mock_root.geometry("400x750")
            self.mock_root.configure(bg=COLOR_BG_MAIN)
            self.mock_root.attributes("-topmost", True)
            self.mock_root.resizable(False, False)

            container = tk.Frame(self.mock_root, bg=COLOR_BG_MAIN, padx=20, pady=20)
            container.pack(fill=tk.BOTH, expand=True)

            outer_t, inner_t = create_card(container, "仮想入力 (トリガー)")
            outer_t.pack(fill=tk.X, pady=(0, 15))

            for t in self.settings.data["gpio"]["triggers"]:
                btn = tk.Button(inner_t, text=f"{t['name']} (ピン {t['pin']})",
                                font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                                activebackground=COLOR_ACCENT, activeforeground="black",
                                relief="flat", cursor="hand2",
                                command=lambda p=t['pin']: self._pulse_mock_input(p))
                btn.pack(fill=tk.X, pady=4)
                Tooltip(btn, "ボタンを押している間だけ入力がONになります")

            outer_p, inner_p = create_card(container, "仮想入力 (パターン決定)")
            outer_p.pack(fill=tk.X, pady=(0, 15))

            self.mock_selectors = {}
            for s in self.settings.data["gpio"].get("pattern_pins", []):
                f = tk.Frame(inner_p, bg=COLOR_BG_PANEL)
                f.pack(fill=tk.X, pady=2)
                
                var = tk.BooleanVar(value=MockManager.get_input_state(s['pin']))
                cb = tk.Checkbutton(f, text=f"{s['name']} (ピン {s['pin']})",
                                    font=FONT_NORMAL, variable=var, 
                                    bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
                                    selectcolor=COLOR_BG_INPUT, activebackground=COLOR_BG_PANEL,
                                    activeforeground=COLOR_TEXT_MAIN, relief="flat",
                                    command=lambda p=s['pin'], v=var: MockManager.set_input(p, v.get()))
                cb.pack(side=tk.LEFT)
                self.mock_selectors[s['pin']] = var

            outer_s, inner_s = create_card(container, "仮想出力")
            outer_s.pack(fill=tk.X)

            self.mock_indicators = {}
            for name, pin, color in [("OK出力", self.settings.data["gpio"]["outputs"]["ok"], COLOR_OK),
                                     ("NG出力", self.settings.data["gpio"]["outputs"]["ng"], COLOR_NG)]:
                f = tk.Frame(inner_s, bg=COLOR_BG_PANEL)
                f.pack(fill=tk.X, pady=8)
                
                lbl = tk.Label(f, text=name, font=FONT_NORMAL, bg=COLOR_BG_PANEL, 
                               fg=COLOR_TEXT_MAIN, width=12, anchor="w")
                lbl.pack(side=tk.LEFT)
                
                outer_ind = tk.Frame(f, width=24, height=24, bg=COLOR_BG_INPUT, padx=2, pady=2)
                outer_ind.pack(side=tk.RIGHT)
                outer_ind.pack_propagate(False)

                ind = tk.Frame(outer_ind, bg="#444")
                ind.pack(fill=tk.BOTH, expand=True)
                self.mock_indicators[str(pin)] = (ind, color)

            low_f = tk.Frame(container, bg=COLOR_BG_MAIN)
            low_f.pack(fill=tk.X, pady=(20, 0))
            tk.Label(low_f, text="※Windowsデバッグ専用機能です", 
                     font=(FONT_FAMILY, 9), bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB).pack()

            self._update_mock_ui()
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル初期化エラー: {e}")

    def _pulse_mock_input(self, pin):
        def _pulse():
            MockManager.set_input(pin, True)
            time.sleep(0.2)
            MockManager.set_input(pin, False)
        threading.Thread(target=_pulse, daemon=True).start()

    def _update_mock_ui(self):
        try:
            if not hasattr(self, "mock_indicators") or not hasattr(self, "mock_root"):
                return
            if not self.mock_root.winfo_exists():
                return
            for pin, ind_data in self.mock_indicators.items():
                ind, color = ind_data
                state = MockManager.get_output_state(pin)
                ind.configure(bg=color if state else "#444")
            if hasattr(self, "mock_selectors"):
                for pin, var in self.mock_selectors.items():
                    current = MockManager.get_input_state(pin)
                    if var.get() != current:
                        var.set(current)
            self.mock_root.after(200, self._update_mock_ui)
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル更新エラー: {e}")

    def load_model(self):
        """モデルの事前案内ログを出力します"""
        self.logger.info("PatchCore検査モードが有効です。検査パターン/トリガー/カメラごとにモデルファイルが個別ロードされます。")

    def get_patchcore_model(self, model_path):
        """メモリ上にPatchCoreモデルをキャッシュロードして返します"""
        if not TORCH_AVAILABLE:
            self.logger.warning("PyTorchがロードできません。推論はスキップされます。")
            return None
        if not model_path:
            return None
            
        path_obj = Path(model_path)
        if not path_obj.exists() or not path_obj.is_file():
            self.logger.warning(f"モデルファイルが見つかりません: {model_path}")
            return None

        if model_path in self.patchcore_models:
            return self.patchcore_models[model_path]

        try:
            self.logger.info(f"AIモデル(PatchCore/PaDiM)をロード中: {model_path}")
            from anomalib.models import Patchcore, Padim
            device = "cuda" if torch.cuda.is_available() else "cpu"

            # 設定 JSON が存在すれば model_type を確認
            model_type = "patchcore"
            json_path = path_obj.parent / "optimal_settings.json"
            if json_path.exists():
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                        model_type = cfg.get("model_type", "patchcore").lower()
                except Exception:
                    pass

            with self.model_lock:
                if "padim" in model_type or "padim" in path_obj.name.lower():
                    try:
                        model = Padim.load_from_checkpoint(model_path)
                    except Exception:
                        model = Patchcore.load_from_checkpoint(model_path)
                else:
                    try:
                        model = Patchcore.load_from_checkpoint(model_path)
                    except Exception:
                        model = Padim.load_from_checkpoint(model_path)

                model = model.to(device)
                model.eval()
            self.patchcore_models[model_path] = model
            self.logger.info(f"AIモデルのロード完了 ({device}): {model_path}")
            return model
        except Exception as e:
            self.logger.error(f"AIモデルのロードに失敗しました: {model_path} - {e}")
            return None

    def predict_patchcore(self, model, cv_img):
        """
        単一のCV画像に対してPatchCore推論を実行し、
        アノマリスコア(float)とアノマリーマップ(Tensor/Array)を返します。
        """
        if model is None:
            return 0.0, None

        try:
            # BGRからRGBに変換
            rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            device = next(model.parameters()).device

            # 各種バージョンのAnomalibに柔軟に対応した前処理の選択
            transform = None
            for attr in ["pre_processor", "transform", "data_transforms"]:
                if hasattr(model, attr):
                    transform = getattr(model, attr)
                    break

            if transform is not None:
                try:
                    transformed = transform(pil_img)
                except Exception:
                    try:
                        transformed = transform(image=rgb)["image"]
                    except Exception:
                        transformed = transform(rgb)

                if isinstance(transformed, dict):
                    tensor = transformed.get("image") or transformed.get("pixel_values")
                else:
                    tensor = transformed
            else:
                # 前処理がない場合のフォールバック（256x256リサイズ＋正規化）
                from torchvision.transforms import functional as F
                resized = F.resize(pil_img, (256, 256))
                tensor = F.to_tensor(resized)
                tensor = F.normalize(tensor, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

            if tensor is None:
                return 0.0, None

            # numpy.ndarray の場合は torch.Tensor に変換する
            if isinstance(tensor, np.ndarray):
                tensor = torch.from_numpy(tensor)
                if tensor.ndim == 3 and tensor.shape[2] in (1, 3, 4):
                    # HWC → CHW
                    tensor = tensor.permute(2, 0, 1)
                tensor = tensor.float()
                if tensor.max() > 1.0:
                    tensor = tensor / 255.0

            if tensor.ndim == 3:
                tensor = tensor.unsqueeze(0)
            tensor = tensor.to(device)

            # 推論実行
            model.eval()
            with torch.no_grad():
                outputs = model(tensor)

            # 結果データの安全な取り出し
            anomaly_score = 0.0
            anomaly_map = None

            if isinstance(outputs, dict):
                anomaly_score = outputs.get("pred_score") or outputs.get("pred_scores") or outputs.get("anomaly_score") or 0.0
                anomaly_map = outputs.get("anomaly_map") or outputs.get("anomaly_maps")
            elif hasattr(outputs, "pred_score"):
                anomaly_score = outputs.pred_score
                anomaly_map = getattr(outputs, "anomaly_map", None)
            else:
                try:
                    anomaly_score = outputs[0]
                except Exception:
                    pass

            if isinstance(anomaly_score, torch.Tensor):
                anomaly_score = anomaly_score.item()

            return float(anomaly_score), anomaly_map
        except Exception as e:
            self.logger.error(f"PatchCore推論処理で例外が発生しました: {e}")
            return 0.0, None

    def generate_heatmap_overlay(self, cv_img, anomaly_map):
        """アノマリーマップ（異常度の局所分布）をサーモグラフィ風のヒートマップにして元画像に重ね合わせます"""
        if anomaly_map is None:
            return cv_img.copy()

        try:
            if isinstance(anomaly_map, torch.Tensor):
                anomaly_map = anomaly_map.detach().cpu().numpy()

            # 次元数を2次元 [H, W] にスクイーズ
            if anomaly_map.ndim == 4:
                anomaly_map = anomaly_map[0, 0]
            elif anomaly_map.ndim == 3:
                anomaly_map = anomaly_map[0]

            # 異常度の正規化 (0.0〜1.0 -> 0〜255)
            amin, amax = anomaly_map.min(), anomaly_map.max()
            if amax - amin > 1e-5:
                norm_map = (anomaly_map - amin) / (amax - amin)
            else:
                norm_map = anomaly_map
            norm_map = (norm_map * 255).astype(np.uint8)

            # 元画像の大きさにヒートマップをリサイズ
            h, w = cv_img.shape[:2]
            heatmap = cv2.resize(norm_map, (w, h), interpolation=cv2.INTER_LINEAR)

            # JETカラーマップを適用してRGB画像化
            heatmap_color = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)

            # 元画像とカラーヒートマップを60:40でブレンド（重み付け合成）
            overlay = cv2.addWeighted(cv_img, 0.6, heatmap_color, 0.4, 0)
            return overlay
        except Exception as e:
            self.logger.error(f"アノマリーマップ可視化生成エラー: {e}")
            return cv_img.copy()

    def get_results_dir(self):
        path_str = self.settings.data["storage"].get("results_dir", str(RESULTS_DIR))
        return Path(path_str)

    def setup_dirs(self):
        try:
            res_dir = self.get_results_dir()
            res_dir.mkdir(parents=True, exist_ok=True)
            for d in ["OK", "NG", "SKIP", "REC"]:
                (res_dir / "images" / d).mkdir(parents=True, exist_ok=True)
            (res_dir / "logs").mkdir(parents=True, exist_ok=True)
            (res_dir / "csv").mkdir(parents=True, exist_ok=True)
            print(f"ディレクトリ構成を確認しました: {res_dir}")
        except Exception as e:
            msg = f"ディレクトリ生成エラー: {e}\n現在の結果出力先: {self.get_results_dir()}"
            print(msg)
            if hasattr(self, 'logger'):
                self.logger.error(msg)

    def setup_logging(self):
        res_dir = self.get_results_dir()
        log_f = res_dir / "logs" / f"app_{datetime.datetime.now().strftime('%Y%m%d')}.log"
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            handlers=[
                logging.FileHandler(log_f, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def setup_hardware(self):
        prev_paused = getattr(self, 'preview_paused', False)
        self.preview_paused = True
        try:
            if hasattr(self, 'inputs'):
                for d in self.inputs.values():
                    d.close()
            if hasattr(self, 'outputs'):
                for d in self.outputs.values():
                    d.close()
            for c in self.caps.values():
                c.release()

            self.inputs = {}
            self.outputs = {}
            self.caps = {}
            data = self.settings.data

            for t in data["gpio"]["triggers"]:
                dev = DigitalInputDevice(t["pin"], pull_up=True, bounce_time=0.05)
                dev.when_activated = lambda i=t["id"]: self.trigger_queue.put(i)
                self.inputs[t["id"]] = dev

            for s in data["gpio"].get("pattern_pins", []):
                self.inputs[f"sel_{s['id']}"] = DigitalInputDevice(s["pin"], pull_up=True, bounce_time=0.05)

            self.out_ok = OutputDevice(data["gpio"]["outputs"]["ok"])
            self.out_ng = OutputDevice(data["gpio"]["outputs"]["ng"])
            self.outputs = {"ok": self.out_ok, "ng": self.out_ng}

            cap_res = data["storage"]["capture_res"]

            def _open_camera(c, cap_res):
                try:
                    backend = cv2.CAP_V4L2 if sys.platform.startswith('linux') else cv2.CAP_ANY
                    cap = cv2.VideoCapture(int(c["index"]), backend)
                    if cap and cap.isOpened():
                        w, h = map(int, cap_res.split('x'))
                        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                        with self.camera_lock:
                            self.caps[c["id"]] = cap
                        self.logger.info(f"カメラ(インデックス {c['index']})を初期化しました: {c['name']}")
                    else:
                        self.logger.error(f"カメラ(インデックス {c['index']})を開けませんでした")
                except Exception as e:
                    self.logger.error(f"カメラ初期化エラー (インデックス {c['index']}): {e}")

            cam_threads = [threading.Thread(target=_open_camera, args=(c, cap_res), daemon=True)
                           for c in data["cameras"]]
            for t in cam_threads:
                t.start()
            for t in cam_threads:
                t.join(timeout=5.0)  
        except Exception as e:
            self.logger.error(f"ハードウェアエラー: {e}")
        finally:
            self.preview_paused = prev_paused

    def get_current_pattern(self):
        st = []
        for s in self.settings.data["gpio"].get("pattern_pins", []):
            pin_id = f"sel_{s['id']}"
            if pin_id in self.inputs:
                st.append(1 if self.inputs[pin_id].is_active else 0)
            else:
                st.append(0)
        for pid in self.settings.data["pattern_order"]:
            p = self.settings.data["patterns"][pid]
            if p.get("pin_condition") == st:
                return pid
        return None

    def setup_gui(self):
        self.root = tk.Tk()
        self.root.title(f"AI自動検査システム (PatchCore / PaDiM 異常検知版) {VERSION}")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG_MAIN)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.root.update_idletasks()
        try:
            self.root.state("zoomed")          
        except tk.TclError:
            try:
                self.root.attributes('-zoomed', True)
            except tk.TclError:
                w = self.root.winfo_screenwidth()
                h = self.root.winfo_screenheight()
                self.root.geometry(f"{w}x{h}+0+0")

        self.header = tk.Frame(self.root, bg=COLOR_BG_PANEL, height=80)
        self.header.pack(fill=tk.X)

        self.lbl_status = tk.Label(self.header, text="システム待機中", font=FONT_LARGE,
                                   bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
        self.lbl_status.pack(side=tk.LEFT, padx=30, pady=15)

        self.lbl_clock = tk.Label(self.header, text="", font=FONT_LARGE,
                                  bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        self.lbl_clock.pack(side=tk.RIGHT, padx=30)
        self.update_clock()

        btn_help = tk.Button(self.header, text="？", font=FONT_BOLD,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", width=3,
                             command=self.show_main_help)
        btn_help.pack(side=tk.RIGHT, padx=10)
        Tooltip(btn_help, "操作方法を表示します")

        mode_frm = tk.Frame(self.header, bg=COLOR_BG_PANEL)
        mode_frm.pack(side=tk.RIGHT, padx=20)

        self.v_mode = tk.StringVar(
            value=self.settings.data["inference"].get("mode", "inspection"))

        def _set_mode(m):
            self.v_mode.set(m)
            self.settings.data["inference"]["mode"] = m
            self.settings.save_settings()
            self.update_mode_ui()

        self.btn_insp = tk.Button(mode_frm, text="検査モード", font=FONT_BOLD,
                                  width=12, relief="flat",
                                  command=lambda: _set_mode("inspection"))
        self.btn_insp.pack(side=tk.LEFT, padx=5)
        Tooltip(self.btn_insp, "カメラ画像からの異常検知判定を行う検査モードに切り替えます")

        self.btn_rec = tk.Button(mode_frm, text="撮影モード", font=FONT_BOLD,
                                 width=12, relief="flat",
                                 command=lambda: _set_mode("recording"))
        self.btn_rec.pack(side=tk.LEFT, padx=5)
        Tooltip(self.btn_rec, "学習用画像収集用の撮影モードに切り替えます")

        self.update_mode_ui()

        main = tk.Frame(self.root, bg=COLOR_BG_MAIN)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.v_frm_outer, v_frm_inner = create_card(main, "カメラプレビュー")
        self.v_frm_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.v_frm_outer.pack_propagate(False)
        self.cam_labels = {}

        self.v_frm = tk.Frame(v_frm_inner, bg=COLOR_BG_PANEL)
        self.v_frm.pack(fill=tk.BOTH, expand=True)

        cams = self.settings.data["cameras"]
        rows = 2 if len(cams) > 2 else 1
        cols = 2 if len(cams) >= 2 else 1

        for i, c in enumerate(cams):
            cam_display_name = (c.get("name", "") or "").strip() or str(c["id"])
            f = tk.LabelFrame(self.v_frm, text=cam_display_name, font=FONT_BOLD,
                              bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, bd=1, relief="solid")
            f.grid(row=i // cols, column=i % cols, sticky="nsew", padx=5, pady=5)
            l = tk.Label(f, bg="black")
            l.pack(fill=tk.BOTH, expand=True)
            self.cam_labels[c["id"]] = l

        for i in range(rows):
            self.v_frm.rowconfigure(i, weight=1)
        for i in range(cols):
            self.v_frm.columnconfigure(i, weight=1)

        pnl_outer, pnl = create_card(main, "操作パネル")
        pnl_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))
        pnl_outer.config(width=420)
        pnl_outer.pack_propagate(False)

        tk.Label(pnl, text="コミット番号", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(5, 2))
        cf = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        cf.pack(fill=tk.X, padx=10, pady=5)
        btn_minus = tk.Button(cf, text="－", font=FONT_LARGE, bg=COLOR_BG_INPUT,
                              fg=COLOR_TEXT_MAIN, width=3, relief="flat",
                              command=lambda: self.adjust_commit(-1))
        btn_minus.pack(side=tk.LEFT, fill=tk.Y)
        Tooltip(btn_minus, "コミット番号を1減らします")

        btn_plus = tk.Button(cf, text="＋", font=FONT_LARGE, bg=COLOR_BG_INPUT,
                             fg=COLOR_TEXT_MAIN, width=3, relief="flat",
                             command=lambda: self.adjust_commit(1))
        btn_plus.pack(side=tk.RIGHT, fill=tk.Y)
        Tooltip(btn_plus, "コミット番号を1増やします")

        self.v_commit = tk.StringVar(value="0001")
        self.lbl_commit = tk.Label(cf, textvariable=self.v_commit,
                                   bg=COLOR_BG_INPUT, fg=COLOR_ACCENT)
        self.update_commit_display()
        self.lbl_commit.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6)
        Tooltip(self.lbl_commit, "現在のコミット番号（検査サイクル識別番号）です")

        btn_num = tk.Button(pnl, text="番号入力", font=FONT_NORMAL, bg="#546E7A",
                            fg="white", relief="flat",
                            command=self.manual_commit_set)
        btn_num.pack(fill=tk.X, padx=10, pady=5)
        Tooltip(btn_num, "テンキーダイアログを開いてコミット番号を直接指定します")

        tk.Label(pnl, text="現在パターン", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(10, 2))
        self.v_pat_name = tk.StringVar(value="---")

        pat_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        pat_frm.pack(fill=tk.X, padx=10)
        tk.Label(pat_frm, textvariable=self.v_pat_name, font=FONT_LARGE,
                 bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=5).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_queue = tk.Button(pat_frm, text="以降一覧", font=FONT_NORMAL, bg="#546E7A",
                                   fg="white", relief="flat", command=self.show_queue_list)
        self.btn_queue.pack(side=tk.LEFT, padx=(5, 0), fill=tk.Y)
        Tooltip(self.btn_queue, "遅延適用される待機中のパターン一覧を表示します")

        bottom_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        bottom_frm.pack(side=tk.BOTTOM, fill=tk.X, pady=(5, 5))

        btn_settings = tk.Button(bottom_frm, text="詳細設定", font=FONT_BOLD, bg="#455A64",
                                 fg="white", height=2, relief="flat", command=self.open_settings)
        btn_settings.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
        Tooltip(btn_settings, "システム・カメラ・GPIO・パターンの詳細設定ダイアログを開きます")

        btn_buzzer = tk.Button(bottom_frm, text="ブザー停止", font=FONT_BOLD, bg=COLOR_NG,
                               fg="white", height=2, relief="flat", command=self.stop_buzzer)
        btn_buzzer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(10, 5))
        Tooltip(btn_buzzer, "NG検出時のGPIO出力およびアラームブザーを強制停止します")

        hist_btn_frm = tk.Frame(bottom_frm, bg=COLOR_BG_PANEL)
        hist_btn_frm.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)
        btn_clear_hist = tk.Button(hist_btn_frm, text="履歴リセット", font=FONT_NORMAL, bg="#546E7A",
                                   fg="white", relief="flat", command=self.clear_history)
        btn_clear_hist.pack(side=tk.LEFT, fill=tk.X, expand=True)
        Tooltip(btn_clear_hist, "画面上のNG履歴リストを消去します")

        btn_res_dir = tk.Button(hist_btn_frm, text="結果フォルダ", font=FONT_NORMAL, bg="#546E7A",
                                fg="white", relief="flat", command=self.open_results_folder)
        btn_res_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        Tooltip(btn_res_dir, "結果画像やログが保存される結果フォルダをファイルマネージャーで開きます")

        lbl_history_title = tk.Label(pnl, text="NG履歴 (ダブルクリックで確認)", font=FONT_BOLD,
                                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB)
        lbl_history_title.pack(side=tk.TOP, pady=(10, 2))

        h_frm = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        h_frm.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 5))

        self.lb_history = tk.Listbox(h_frm, font=(FONT_FAMILY, 14),
                                     bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                                     selectbackground=COLOR_ACCENT,
                                     selectforeground="black", relief="flat")
        self.lb_history.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.lb_history.bind("<Double-Button-1>", self.on_history_double_click)

        sb = tk.Scrollbar(h_frm, orient=tk.VERTICAL, command=self.lb_history.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.lb_history.config(yscrollcommand=sb.set)

        self.root.app_instance = self
        threading.Thread(target=self._preview_loop, daemon=True).start()
        threading.Thread(target=self._main_logic_loop, daemon=True).start()

    def show_queue_list(self):
        d = self.settings.data
        patterns = d.get("patterns", {})
        queue = list(self.delay_pattern_queue)
        delay_cycles = float(d.get("system", {}).get("delay_cycles", 0))

        win = tk.Toplevel(self.root)
        win.title("遅延パターン一覧")
        win.configure(bg=COLOR_BG_MAIN)
        win.transient(self.root)
        win.geometry("480x420")
        win.resizable(True, True)

        hdr = tk.Frame(win, bg=COLOR_BG_PANEL, pady=10)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="遅延キュー　待機パターン一覧", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_ACCENT).pack(padx=20, anchor="w")
        tk.Label(hdr, text=f"遅延サイクル数: {delay_cycles}　キュー長: {len(queue)}",
                 font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(padx=20, anchor="w")

        body = tk.Frame(win, bg=COLOR_BG_MAIN, padx=15, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        lb_frm = tk.Frame(body, bg=COLOR_BG_MAIN)
        lb_frm.pack(fill=tk.BOTH, expand=True)

        lb = tk.Listbox(lb_frm, font=(FONT_FAMILY, 14),
                        bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                        selectbackground=COLOR_ACCENT, selectforeground="black",
                        relief="flat")
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = tk.Scrollbar(lb_frm, orient=tk.VERTICAL, command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)

        if not queue:
            lb.insert(tk.END, "（待機中のパターンはありません）")
        else:
            for i, pid in enumerate(queue):
                pat = patterns.get(pid)
                if pid is None:
                    pat_name = "（未確定 / SKIP）"
                elif pat:
                    pat_name = pat.get("name", pid)
                else:
                    pat_name = f"（不明: {pid}）"
                lb.insert(tk.END, f"  {i + 1}. {pat_name}")

        tk.Button(win, text="閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
                  fg=COLOR_TEXT_MAIN, relief="flat", pady=10,
                  command=win.destroy).pack(fill=tk.X, padx=15, pady=(0, 12))

    def on_closing(self):
        if messagebox.askokcancel("終了", "アプリケーションを終了しますか？"):
            self.running = False
            self.logger.info("シャットダウン処理を開始します...")

            if hasattr(self, "mock_root") and self.mock_root.winfo_exists():
                try:
                    self.mock_root.destroy()
                except Exception: pass

            try:
                for d in getattr(self, 'inputs', {}).values():
                    if hasattr(d, 'close'): d.close()
                for d in getattr(self, 'outputs', {}).values():
                    if hasattr(d, 'close'): d.close()
            except Exception as e:
                self.logger.error(f"GPIO解放エラー: {e}")

            try:
                for c in getattr(self, 'caps', {}).values():
                    c.release()
            except Exception as e:
                self.logger.error(f"カメラ解放エラー: {e}")

            if PYGAME_AVAILABLE:
                try:
                    pygame.mixer.quit()
                except Exception: pass

            self.root.destroy()
            self.logger.info("シャットダウン完了")

    def update_clock(self):
        now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        self.lbl_clock.config(text=now)
        self.root.after(1000, self.update_clock)

    def _monitor_storage(self):
        _INTERVAL_MS = 10 * 60 * 1000  
        
        def _thread_task():
            try:
                st = self.settings.data.get("storage", {})
                if not st.get("auto_delete_enabled", False):
                    return

                max_gb = float(st.get("max_results_gb", 0))
                if max_gb <= 0:
                    return

                res_dir = Path(self.get_results_dir())
                images_dir = res_dir / "images"
                if not images_dir.exists():
                    return

                img_files = sorted(
                    [f for f in images_dir.rglob("*") if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")],
                    key=lambda f: f.stat().st_mtime
                )
                total_size = sum(f.stat().st_size for f in img_files)

                import shutil
                usage = shutil.disk_usage(res_dir)
                free_gb = usage.free / (1024 ** 3)
                max_bytes = max_gb * (1024 ** 3)

                needs_deletion = False
                target_bytes = total_size

                if total_size > max_bytes:
                    needs_deletion = True
                    target_bytes = max_bytes * 0.9  
                elif free_gb < 1.0:
                    needs_deletion = True
                    target_bytes = max(0, total_size - (1.0 * 1024**3))

                if not needs_deletion:
                    return

                self.logger.info(f"[容量監視] 削除開始。画像サイズ: {total_size/(1024**3):.2f} GB / 空き容量: {free_gb:.2f} GB")

                deleted_count = 0
                for f in img_files:
                    if total_size <= target_bytes:
                        break
                    try:
                        file_size = f.stat().st_size
                        f.unlink()
                        total_size -= file_size
                        deleted_count += 1
                    except Exception as e:
                        self.logger.warning(f"[容量監視] 削除失敗: {f.name} - {e}")

                if deleted_count > 0:
                    self.logger.info(f"[容量監視] {deleted_count} 件削除完了。残画像サイズ: {total_size/(1024**3):.2f} GB")
            except Exception as e:
                self.logger.error(f"[容量監視] エラー: {e}")
            finally:
                if self.running:
                    self.root.after(_INTERVAL_MS, self._monitor_storage)

        t = threading.Thread(target=_thread_task, daemon=True)
        t.start()

    def get_commit_str(self, for_ui=False):
        st_sys = self.settings.data.get("system", {})
        is_half_step = bool(st_sys.get("commit_half_step", False))
        if is_half_step:
            is_fr = (self.commit_number % 1.0 < 0.25)
            tag = "Fr" if is_fr else "Rr"
            c_int = int(self.commit_number)
            if for_ui:
                return f"{c_int:04d} {tag}"
            return f"{c_int:04d}{tag}"
        else:
            return f"{int(self.commit_number):04d}"

    def adjust_commit(self, delta):
        st_sys = self.settings.data.get("system", {})
        is_half_step = bool(st_sys.get("commit_half_step", False))
        step = 0.5 if is_half_step else 1.0

        self.commit_number += delta * step
        if self.commit_number > 9999.0:
            self.commit_number = 1.0
        elif self.commit_number < 1.0:
            self.commit_number = 9999.0
        self.root.after(0, lambda: self.v_commit.set(self.get_commit_str(for_ui=True)))

    def update_commit_display(self):
        commit_font, commit_width = get_commit_display_style(
            bool(self.settings.data.get("system", {}).get("commit_half_step", False))
        )
        self.lbl_commit.config(font=commit_font, width=commit_width)
        self.v_commit.set(self.get_commit_str(for_ui=True))

    def manual_commit_set(self):
        is_half_step = bool(self.settings.data.get("system", {}).get("commit_half_step", False))
        d = TenKeyDialog(self.root, "コミット番号設定", self.commit_number, is_half_step)
        if d.result is not None:
            self.commit_number = float(d.result)
            self.v_commit.set(self.get_commit_str(for_ui=True))

    def manual_commit_set_initial(self):
        try:
            is_half_step = bool(self.settings.data.get("system", {}).get("commit_half_step", False))
            d = TenKeyDialog(self.root, "開始コミット番号", self.commit_number, is_half_step)
            if d.result is not None:
                self.commit_number = float(d.result)
                self.v_commit.set(self.get_commit_str(for_ui=True))
        except Exception as e:
            self.logger.error(f"初期コミット番号設定エラー: {e}")

    def stop_buzzer(self):
        if self.out_ng:
            self.out_ng.off()
        if PYGAME_AVAILABLE and pygame.mixer.get_init():
            pygame.mixer.music.stop()

    def clear_history(self):
        if messagebox.askyesno("確認", "NG履歴を削除しますか？"):
            self.ng_history.clear()
            self.lb_history.delete(0, tk.END)

    def open_results_folder(self):
        folder = self.get_results_dir() / "images"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform.startswith("linux"):
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
            else:
                import subprocess
                subprocess.Popen(["open", str(folder)])
        except Exception as e:
            self.logger.error(f"結果フォルダを開けませんでした: {e}")
            messagebox.showerror("エラー", f"フォルダを開けませんでした:\n{folder}", parent=self.root)

    def on_history_double_click(self, e):
        s = self.lb_history.curselection()
        if not s:
            return
        rec = self.ng_history[len(self.ng_history) - 1 - s[0]]
        res_dir = self.get_results_dir()
        dir_ng = res_dir / "images" / "NG"

        commit_str = rec.get("commit_str")
        if not commit_str:
            try:
                commit_str = f"{int(rec['commit']):04d}"
            except:
                commit_str = str(rec['commit'])
        all_imgs = sorted(dir_ng.glob(f"NG_{commit_str}_*"))

        rec_time = rec.get("time")
        if rec_time is not None:
            rec_date = rec_time.date()
            imgs = [
                f for f in all_imgs
                if datetime.datetime.fromtimestamp(f.stat().st_mtime).date() == rec_date
            ]
            if not imgs:
                imgs = all_imgs
        else:
            imgs = all_imgs

        if not imgs:
            messagebox.showinfo("情報", f"#{commit_str} の画像ファイルが見つかりません。\n保存先: {dir_ng}")
            return

        top = tk.Toplevel(self.root)
        top.title(f"NG詳細 #{commit_str} ({len(imgs)}枚)")
        top.configure(bg=COLOR_BG_MAIN)
        top.transient(self.root)

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        win_w = int(sw * 0.85)
        win_h = int(sh * 0.85)
        top.geometry(f"{win_w}x{win_h}")

        tk.Label(top, text=f"NG #{commit_str} — {len(imgs)}枚", font=FONT_LARGE,
                 bg=COLOR_BG_MAIN, fg=COLOR_NG).pack(pady=(10, 0))

        frame_outer = tk.Frame(top, bg=COLOR_BG_MAIN)
        frame_outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        canvas_scroll = tk.Canvas(frame_outer, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = tk.Scrollbar(frame_outer, orient=tk.VERTICAL, command=canvas_scroll.yview)
        canvas_scroll.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas_scroll, bg=COLOR_BG_MAIN)
        canvas_scroll.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event):
            canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
        inner.bind("<Configure>", _on_inner_configure)

        def _on_mousewheel(event):
            canvas_scroll.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas_scroll.bind_all("<MouseWheel>", _on_mousewheel)
        top.bind("<Destroy>", lambda e: canvas_scroll.unbind_all("<MouseWheel>"))

        img_max_w = int(win_w * 0.82)
        img_max_h = int(sh * 0.65)

        for f in imgs:
            try:
                im = Image.open(f)
                im.thumbnail((img_max_w, img_max_h), Image.LANCZOS)
                t_im = ImageTk.PhotoImage(im)

                tk.Label(inner, text=f.name, font=FONT_NORMAL,
                         bg=COLOR_BG_MAIN, fg=COLOR_TEXT_SUB).pack(anchor="w", padx=10, pady=(10, 2))
                lbl = tk.Label(inner, image=t_im, bg=COLOR_BG_MAIN)
                lbl.image = t_im  
                lbl.pack(padx=10, pady=(0, 5))
            except Exception as ex:
                self.logger.error(f"NG画像読み込みエラー: {f.name} - {ex}")

        tk.Button(top, text="閉じる", font=FONT_BOLD, bg="#546E7A", fg="white",
                  relief="flat", padx=20,
                  command=top.destroy).pack(pady=10)

    def show_main_help(self):
        help_data = {
            "概要": "AI (PatchCore / PaDiM 異常検知) を用いた、正常・異常判定システムです。\n\n【基本的な流れ】\n1. 設定画面でカメラや判定条件（モデル、しきい値）を登録する。\n2. コミット番号を設定する。\n3. トリガー待ち状態になります。設定された順序（上から順）に外部信号が入ると撮影・判定が行われます。",
            "検査モード": "自動判定を行う通常モードです。\n・トリガーが順に入ると判定が開始されます。\n・画像のアノマリスコアがモデルごとの「しきい値」未満ならOK（正常）、以上ならNG（異常）信号を出力します。\n・NG時はヒートマップ付きの画像と元画像（NG_RAW）を自動保存します。",
            "撮影モード": "判定を行わず、画像を収集するモードです。\n・リトライ回数分の画像を全て保存し、学習用（PatchCore / PaDiM モデル作成アプリ等）データの収集に使用します。",
            "コミット番号": "ファイル名やログに含まれる管理番号です。\n・1サイクル（全トリガー完了）ごとに自動で+1されます。\n・「番号入力」から手動設定も可能です。\n・ドアライン対応: 設定画面で「0.5刻み」を有効にすると、コミット番号が0.5刻みで進みます。",
            "NG履歴とお知らせ": "最近のNG（異常検出）判定が簡易表示されます。\n・ダブルクリックで画像を確認できます。\n・ステータスバーには現在の「撮影中」「検査中」などの状態が表示されます。",
            "AIモデル・パターン設定": "設定画面の「パターン」タブで設定します。\n・カメラごと、トリガーごと個別に PatchCore または PaDiM の `.ckpt` モデルファイルを指定できます。\n・「判定しきい値」: この値以上にアノマリスコアが高くなると、異常（Abnormal/NG）とみなします。\n・「テストボタン」: 現在のカメラライブ映像に、指定したモデルでのリアルタイムの異常度ヒートマップを重ね合わせて合否プレビューできます。"
        }
        HelpWindow(self.root, "操作ヘルプ", help_data)

    def open_settings(self):
        self.settings_open = True
        self.logger.info("設定画面を開きました。設定画面が閉じるまで検査処理をスキップします。")
        self.root.update_idletasks()
        SettingsDialog(self.root, self.settings, self.on_settings_closed)

    def reset_delay_pattern_queue(self):
        self.delay_pattern_queue.clear()
        self.elapsed_cycles = 0.0
        self.cycle_is_delayed_skip = False
        if self.cycle_active_pat_id == DELAYED_SKIP_PATTERN_ID:
            self.cycle_active_pat_id = None
        self.logger.info("仕様情報遅延キューをリセットしました")

    def on_settings_closed(self):
        self.settings_open = False
        self.logger.info("設定画面が閉じられました。検査処理を再開します。")
        self.setup_hardware()
        self.v_mode.set(self.settings.data["inference"].get("mode", "inspection"))
        self.update_commit_display()
        self.update_mode_ui()

    def update_mode_ui(self):
        m = self.v_mode.get()
        if m == "inspection":
            self.btn_insp.config(bg=COLOR_ACCENT, fg="black")
            self.btn_rec.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB)
            self.update_status("検査モード 待機中", COLOR_BG_PANEL)
        else:
            self.btn_insp.config(bg=COLOR_BG_INPUT, fg=COLOR_TEXT_SUB)
            self.btn_rec.config(bg=COLOR_WARNING, fg="black")
            self.update_status("撮影モード 実行中", COLOR_ACCENT)

    def _preview_loop(self):
        while self.running:
            if self.preview_paused or self.inspecting:
                time.sleep(0.1)
                continue

            if time.time() < self.result_display_until:
                for cid, pil_img in list(self.result_display_frames.items()):
                    if not getattr(self.cam_labels.get(cid), 'is_updating', False):
                        self.cam_labels[cid].is_updating = True
                        def _upd_static(c=cid, img_data=pil_img):
                            if c in self.cam_labels:
                                try:
                                    tk_img = ImageTk.PhotoImage(img_data)
                                    self.cam_labels[c].config(image=tk_img)
                                    self.cam_labels[c].img = tk_img
                                except Exception: pass
                                finally:
                                    self.cam_labels[c].is_updating = False
                        self.root.after(0, _upd_static)
                time.sleep(0.1)
                continue

            t_start = time.time()
            
            with self.camera_lock:
                current_caps = list(self.caps.items())
            
            for cid, cap in current_caps:
                if self.preview_paused or self.inspecting:
                    break
                try:
                    with self.camera_lock:
                        if cid not in self.caps:
                            continue
                        grabbed = cap.grab()
                        if grabbed:
                            ret, frame = cap.retrieve()
                        else:
                            ret = False
                    if grabbed and ret:
                        self.last_frames[cid] = frame  
                        if not getattr(self.cam_labels.get(cid), 'is_updating', False):
                            self.cam_labels[cid].is_updating = True
                            
                            def _upd_live(c=cid, f_data=frame):
                                if c in self.cam_labels:
                                    try:
                                        preview_res = self.settings.data["storage"].get("preview_res", "320x240")
                                        if preview_res != "プレビューなし":
                                            try:
                                                pw, ph = map(int, preview_res.split('x'))
                                            except Exception:
                                                pw, ph = 320, 240
                                            
                                            img = cv2.resize(f_data, (pw, ph), interpolation=cv2.INTER_LINEAR)
                                            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                                            pil_img = Image.fromarray(img)
                                            
                                            tk_img = ImageTk.PhotoImage(pil_img)
                                            self.cam_labels[c].config(image=tk_img)
                                            self.cam_labels[c].img = tk_img
                                    except Exception: pass
                                    finally:
                                        self.cam_labels[c].is_updating = False

                            self.root.after(0, _upd_live)
                except Exception as e:
                    self.logger.error(f"Preview error (cid={cid}): {e}")

            fps = self.settings.data["inference"].get("preview_fps", 10)
            elapsed = time.time() - t_start
            wait_time = max(0.01, (1.0 / max(0.1, float(fps))) - elapsed)
            time.sleep(wait_time)

    def save_result_images(self, result_type, frame, camera_name, pattern_name,
                           confidence=1.0, trig_name="Trig1", burst_index=None):
        if frame is None:
            return None

        if result_type == "REC":
            res_key = "res_record"
        elif result_type == "NG_RAW":
            res_key = "res_ng"
        else:
            res_key = f"res_{result_type.lower()}"
        res_setting = self.settings.data["storage"].get(res_key, "640x480")
        
        if res_setting == "保存しない":
            return None
            
        save_frame = frame
        if "x" in res_setting:
            try:
                w, h = map(int, res_setting.split("x"))
                save_frame = cv2.resize(frame, (w, h))
            except Exception: pass

        b_suffix = f"_{burst_index:02d}" if burst_index is not None else ""
        filename = (f"{result_type}_{self.get_commit_str()}_{pattern_name}_"
                    f"{camera_name}_{trig_name}{b_suffix}_{confidence:.4f}.jpg")

        filename = "".join([c for c in filename if c not in '<>:"/\\|?*'])
        res_dir = self.get_results_dir()

        # 撮影モード(REC)はパターン・カメラ・トリガーごとのサブフォルダに保存
        if result_type == "REC":
            subfolder_name = f"{pattern_name}_{camera_name}_{trig_name}"
            subfolder_name = "".join([c for c in subfolder_name if c not in '<>:"/\\|?*'])
            save_dir = res_dir / "images" / result_type / subfolder_name
        else:
            save_dir = res_dir / "images" / result_type
        save_dir.mkdir(parents=True, exist_ok=True)
        
        save_path = save_dir / filename
        
        def _do_write(path, img, fname):
            try:
                ext = path.suffix or ".jpg"
                result, buf = cv2.imencode(ext, img)
                if result:
                    with open(path, "wb") as f:
                        f.write(buf)
                    self.logger.info(f"保存成功: {fname}")
                else:
                    self.logger.error(f"画像エンコード失敗: {path}")
            except Exception as e:
                self.logger.error(f"保存失敗: {path} - {e}")
        threading.Thread(target=_do_write, args=(save_path, save_frame, filename), daemon=True).start()
            
        return save_path

    def append_to_csv(self, pattern_name, camera_name, class_name, detected_count, res_type, confidence):
        today = datetime.datetime.now().strftime('%Y%m%d')
        now_time = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
        res_dir = self.get_results_dir()
        csv_dir = res_dir / "csv"
        csv_dir.mkdir(parents=True, exist_ok=True)
        csv_file = csv_dir / f"inspection_results_{today}.csv"

        file_exists = csv_file.exists()
        # カラム名のマッピング（PatchCore向けに調整）
        header = ["日時", "コミット番号", "パターン名", "カメラ名", "判定しきい値", "算出スコア", "判定結果", "アノマリスコア"]
        data = [now_time, self.get_commit_str(), pattern_name, camera_name, class_name, detected_count, res_type, f"{confidence:.4f}"]

        def _do_csv(path, row, hdr, needs_hdr):
            try:
                with open(path, 'a', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                    if needs_hdr:
                        writer.writerow(hdr)
                    writer.writerow(row)
            except Exception as e:
                self.logger.error(f"CSV書き込みエラー: {e}")

        threading.Thread(target=_do_csv, args=(csv_file, data, header, file_exists is False), daemon=True).start()

    def _capture_burst_images(self, retries, interval):
        captured_frames = []
        d = self.settings.data
        cam_names = {
            c["id"]: (c.get("name", "") or "").strip() or str(c["id"])
            for c in d["cameras"]
        }

        for shot_idx in range(max(1, retries)):
            with self.camera_lock:
                if shot_idx == 0:
                    for cid, cap in self.caps.items():
                        for _ in range(3):
                            cap.grab()

                grabbed = {}  
                for cid, cap in self.caps.items():
                    if cap.grab():
                        grabbed[cid] = cap  

            shot_data = []
            for cid, cap in grabbed.items():
                ret, frame = cap.retrieve()
                if ret:
                    shot_data.append((cid, cam_names.get(cid, cid), frame))

            if shot_data:
                captured_frames.append(shot_data)
            if shot_idx < retries - 1 and interval > 0:
                time.sleep(interval)
        return captured_frames

    def _inspect_frames(self, captured_frames, mode, is_skip, pat_id, trig_id, pat_name, trig_name):
        """
        [PatchCore対応版] 
        各カメラ個別に、設定されたPatchCoreモデルをロードしてアノマリスコアベースの合否判定を行います。
        """
        d = self.settings.data
        final_best_frames = {}
        
        # conditions のキャッシュ化（カメラIDごとのモデル条件の取得）
        # c_cond 期待フォーマット: {"model_path": "path/to/pc.ckpt", "threshold": 0.49}
        conditions_cache = {}  
        if not is_skip and pat_id:
            st_sys = self.settings.data.get("system", {})
            is_half_step = bool(st_sys.get("commit_half_step", False))
            is_fr = (self.commit_number % 1.0 < 0.25)
            stage = self.settings.data["patterns"][pat_id]["stages"].get(trig_id, {})

            if is_half_step:
                cond_key = "conditions_fr" if is_fr else "conditions_rr"
                cond_data = stage.get(cond_key)
                if cond_data is None:
                    cond_data = stage.get("conditions", {})
            else:
                cond_data = stage.get("conditions", {})

            for cam in self.settings.data["cameras"]:
                cid = cam["id"]
                if isinstance(cond_data, dict):
                    conditions_cache[cid] = cond_data.get(str(cid), {})
                else:
                    conditions_cache[cid] = {}

        all_cids = set()
        if captured_frames:
            for cid, cam_name, frame in captured_frames[0]:
                all_cids.add(cid)

        satisfied_cameras = set()  

        for burst_idx, shot_group in enumerate(captured_frames):
            for cid, cam_name, frame in shot_group:
                if cid in satisfied_cameras:
                    continue

                if frame is None or not hasattr(frame, "shape") or frame.size == 0 or len(frame.shape) < 2 or frame.shape[0] == 0 or frame.shape[1] == 0:
                    self.logger.warning(
                        f"不正な画像フレームを検出 (カメラ: {cam_name})。スキップします。"
                    )
                    continue

                # カメラごとのPatchCore設定を取得
                cond = conditions_cache.get(cid, {})
                model_path = cond.get("model_path", "").strip()
                threshold = float(cond.get("threshold", 0.49))

                # モデルが未指定（未選択）またはファイル非存在の場合は検査実施せずSKIP（対象外）として扱う
                is_this_cam_skip = is_skip or not model_path or not os.path.exists(model_path)

                if mode == "recording":
                    save_needed = not (is_this_cam_skip and d["storage"].get("res_record_skip", "") == "保存しない")
                    if save_needed:
                        self.save_result_images("REC", frame, cam_name, pat_name,
                                                trig_name=trig_name, burst_index=burst_idx + 1)
                    final_best_frames[(cid, cam_name)] = (frame, frame, "OK", 0.0, "-", "0")
                else:
                    # ---- PatchCore推論処理 ----
                    score = 0.0
                    anomaly_map = None
                    frame_to_save = frame

                    if is_this_cam_skip:
                        res_type = "SKIP"
                        cond_summary = "-"
                        det_summary = "非対象 (SKIP)"
                    else:
                        model_pc = self.get_patchcore_model(model_path)
                        if model_pc is not None:
                            try:
                                score, anomaly_map = self.predict_patchcore(model_pc, frame)
                                is_abnormal = score >= threshold
                                # 正常(OK)、異常(NG)の割り当て
                                res_type = "NG" if is_abnormal else "OK"
                                cond_summary = f"閾値={threshold:.4f}"
                                det_summary = f"スコア={score:.4f}"
                                
                                # アノマリーヒートマップの可視化重ね合わせ画像を生成
                                frame_to_save = self.generate_heatmap_overlay(frame, anomaly_map)
                            except Exception as e:
                                self.logger.error(f"PatchCore推論プロセスでエラー: {e}")
                                res_type = "NG"
                                cond_summary = "推論エラー"
                                det_summary = "ERR"
                        else:
                            # モデルファイルのロード自体に失敗した場合は安全のためNGとする
                            res_type = "NG"
                            cond_summary = "モデル読込エラー"
                            det_summary = "ERR"

                    if res_type in ("OK", "SKIP"):
                        satisfied_cameras.add(cid)

                    is_first_record = (cid, cam_name) not in final_best_frames
                    prev_was_ng = not is_first_record and final_best_frames[(cid, cam_name)][2] == "NG"
                    if is_first_record or res_type == "OK" or prev_was_ng:
                        final_best_frames[(cid, cam_name)] = (frame_to_save, frame, res_type, score, cond_summary, det_summary)

            if mode == "inspection" and all_cids and all_cids.issubset(satisfied_cameras):
                self.logger.info(f"バースト撮影 {burst_idx + 1}回目で全カメラOK/SKIP判定確定。以降のリトライをスキップ")
                break

        if mode == "recording":
            satisfied_cameras.update(all_cids)

        results = [val[2] for val in final_best_frames.values()]
        return results, final_best_frames

    def process_inspection(self, trig_id):
        d = self.settings.data
        inference_cfg = d["inference"]
        mode = inference_cfg.get("mode", "inspection")

        trig_list = [t["id"] for t in d["gpio"]["triggers"]]
        if not trig_list:
            self.logger.warning("トリガーが設定されていません。無視します。")
            return

        expected_trig = trig_list[self.cycle_trig_idx]
        if trig_id != expected_trig:
            expected_name = self._get_trig_name(expected_trig)
            received_name = self._get_trig_name(trig_id)
            self.logger.warning(f"順序外のトリガーを無視: 受信={received_name}, 期待={expected_name}")
            return

        status_msg = "撮影中..." if mode == "recording" else "検査中..."
        self.update_status(status_msg, COLOR_ACCENT)
        
        self.inspecting = True

        if self.cycle_active_pat_id is None and len(self.cycle_fired_trigs) == 0:
            st_sys = self.settings.data.get("system", {})
            is_half_step = bool(st_sys.get("commit_half_step", False))
            is_fr_cycle = (self.commit_number % 1.0 < 0.25)

            if is_half_step and not is_fr_cycle and self.door_latch_pat_id is not None:
                # ドアライン対応の Rr (小数) サイクル: Fr (整数) 時に決定したパターンを引き継ぐ
                self.cycle_active_pat_id = self.door_latch_pat_id
                self.cycle_is_delayed_skip = False
                self.logger.info(
                    f"[ドアライン] Rrサイクル: Frパターンを引き継ぎ適用: {self.door_latch_pat_id}"
                )
            else:
                # 通常または Fr (整数) サイクル: 通常通りパターン決定
                raw_pat_id = self.get_current_pattern()
                delay_cycles = float(st_sys.get("delay_cycles", 0))

                if delay_cycles > 0:
                    self.delay_pattern_queue.append(raw_pat_id)

                    if self.elapsed_cycles < delay_cycles:
                        self.cycle_active_pat_id = DELAYED_SKIP_PATTERN_ID
                        self.cycle_is_delayed_skip = True
                        self.logger.info(
                            f"[遅延キュー] 蓄積中 ({self.elapsed_cycles:.1f}/{delay_cycles:.1f} サイクル完了)。"
                            f" キュー長={len(self.delay_pattern_queue)}"
                        )
                    else:
                        applied_pat_id = self.delay_pattern_queue.pop(0) if self.delay_pattern_queue else None
                        self.cycle_active_pat_id = applied_pat_id
                        self.cycle_is_delayed_skip = False
                        self.logger.info(
                            f"[遅延キュー] パターン適用: {applied_pat_id}。"
                            f" キュー残={len(self.delay_pattern_queue)}"
                        )
                else:
                    self.cycle_active_pat_id = raw_pat_id
                    self.cycle_is_delayed_skip = False

                if is_half_step and is_fr_cycle:
                    self.door_latch_pat_id = self.cycle_active_pat_id

            self.cycle_fired_trigs = set()
            self.cycle_trig_idx = 0 

        pat_id = self.cycle_active_pat_id
        if (
            not pat_id
            or pat_id == DELAYED_SKIP_PATTERN_ID
            or getattr(self, 'cycle_is_delayed_skip', False)
        ):
            pat_name = "SKIP"
            is_skip = True
            required_trig_ids = set(trig_list)
        else:
            pat = d["patterns"][pat_id]
            pat_name = pat.get("name", "").strip() or str(pat_id)
            is_skip = False
            configured_trig_ids = set(t["id"] for t in d["gpio"]["triggers"])
            st_sys = self.settings.data.get("system", {})
            is_half_step = bool(st_sys.get("commit_half_step", False))
            is_fr = (self.commit_number % 1.0 < 0.25)

            if is_half_step:
                cond_key = "conditions_fr" if is_fr else "conditions_rr"
                required_trig_ids = set(
                    tid for tid, stage in pat["stages"].items() 
                    if stage.get(cond_key) or stage.get("conditions")
                ) & configured_trig_ids
            else:
                required_trig_ids = set(
                    tid for tid, stage in pat["stages"].items() 
                    if stage.get("conditions")
                ) & configured_trig_ids
            
            if not required_trig_ids:
                required_trig_ids = {trig_id}

        if len(self.cycle_fired_trigs) == 0:
            self.logger.info(f"--- サイクル開始 (パターン: {pat_name}) ---")

        self.v_pat_name.set(pat_name)
        self.cycle_fired_trigs.add(trig_id)
        self.cycle_trig_idx += 1 
        if self.cycle_trig_idx >= len(trig_list):
            self.cycle_trig_idx = 0 

        trig_info = next((t for t in d["gpio"]["triggers"] if t["id"] == trig_id), None)
        trig_name = trig_info["name"].strip() if trig_info else str(trig_id)

        retries = 1 if is_skip else inference_cfg.get("max_retries", 5)
        interval = inference_cfg.get("burst_interval", 0.5)
        captured_frames = self._capture_burst_images(retries, interval)

        results, final_best_frames = self._inspect_frames(
            captured_frames, mode, is_skip, pat_id, trig_id, pat_name, trig_name
        )

        if mode == "recording":
            self.update_status(f"撮影保存完了 (#{self.get_commit_str()})", COLOR_OK)
            time.sleep(1)
            self.inspecting = False
            self.update_status("撮影モード 待機中", COLOR_ACCENT)
        elif mode == "inspection":
            display_frames = {}
            for (cid, cam_name), (frame, raw_frame, res_type, conf, cls_name, det_cnt) in final_best_frames.items():
                self.logger.info(f"判定結果: カメラ={cam_name}, しきい値={cls_name}, 得点={det_cnt}, 結果={res_type}")
                res_setting = d["storage"].get(f"res_{res_type.lower()}", "640x480")
                if res_setting != "保存しない":
                    self.save_result_images(res_type, frame, cam_name, pat_name, 
                                            confidence=conf, trig_name=trig_name)
                    if res_type == "NG":
                        self.save_result_images(RESULTS_SUBDIR_NG_RAW, raw_frame, cam_name, pat_name,
                                                confidence=conf, trig_name=trig_name)
                self.append_to_csv(pat_name, cam_name, cls_name, det_cnt, res_type, conf)

                if res_type == "NG":
                    self.add_history(trig_id) 

                preview_res = self.settings.data["storage"].get("preview_res", "320x240")
                try:
                    pw, ph = map(int, preview_res.split('x'))
                except Exception: pw, ph = 320, 240
                
                rgb = cv2.cvtColor(cv2.resize(frame, (pw, ph)), cv2.COLOR_BGR2RGB)
                display_frames[cid] = Image.fromarray(rgb)

            self.result_display_frames = display_frames
            display_time = d["inference"].get("result_display_time", 2.0)
            self.result_display_until = time.time() + display_time

        is_cycle_complete = required_trig_ids.issubset(self.cycle_fired_trigs)
        
        if len(trig_list) <= 1:
            is_cycle_complete = True
        elif self.cycle_trig_idx == 0 and len(self.cycle_fired_trigs) >= len(trig_list):
            is_cycle_complete = True

        if is_cycle_complete:
            self.logger.info(f"--- サイクル完了 ({self.get_commit_str()}) ---")

            st_sys = self.settings.data.get("system", {})
            is_half_step = bool(st_sys.get("commit_half_step", False))
            cycle_step = 0.5 if is_half_step else 1.0
            self.elapsed_cycles += cycle_step

            self.adjust_commit(1)    
            self.cycle_active_pat_id = None
            self.cycle_is_delayed_skip = False
            self.cycle_fired_trigs.clear()
            self.cycle_trig_idx = 0  
            self.clear_trigger_queue()  
        else:
            next_trig_id = trig_list[self.cycle_trig_idx]
            next_trig_name = self._get_trig_name(next_trig_id)
            self.logger.info(f"サイクル継続中 (進捗: {len(self.cycle_fired_trigs)}/{len(required_trig_ids)}, 次待機: {next_trig_name})")

        if mode != "inspection":
            return

        ok_time = inference_cfg.get("ok_output_time", 0.5)
        ng_time = inference_cfg.get("ng_output_time", "")
        has_ng = "NG" in results

        if has_ng:
            self.update_status(f"異常(Abnormal)検出 ({pat_name})", COLOR_NG)
            if self.out_ok:
                self.out_ok.off()

            if self.out_ng:
                ng_hold = bool(inference_cfg.get("ng_output_hold", False))
                if ng_hold:
                    self.out_ng.on()
                else:
                    ng_time_str = str(ng_time).strip() if ng_time is not None else ""
                    if ng_time_str == "":
                        self.out_ng.on()
                    else:
                        try:
                            ng_sec = float(ng_time_str)
                            if ng_sec > 0:
                                self.out_ng.on()
                                ng_msec = int(ng_sec * 1000)
                                def _ng_off():
                                    if self.out_ng:
                                        self.out_ng.off()
                                self.root.after(max(10, ng_msec), _ng_off)
                        except ValueError:
                            pass

            bp = inference_cfg.get("buzzer_path", "")
            if bp and PYGAME_AVAILABLE and os.path.exists(bp):
                try:
                    _ensure_mixer()
                    pygame.mixer.music.load(bp)
                    pygame.mixer.music.play(-1)
                except: pass

        elif results and all(r in ("OK", "SKIP") for r in results):
            status_color = COLOR_OK if "OK" in results else COLOR_BG_PANEL

            if "OK" in results:
                self.update_status(f"正常(Normal) OK ({pat_name})", status_color)
                if self.out_ng:
                    try: self.out_ng.off()
                    except: pass
                if self.out_ok:
                    try:
                        self.out_ok.on()
                        ok_msec = int(ok_time * 1000)
                        def _ok_off():
                            try:
                                if self.out_ok: self.out_ok.off()
                            except: pass
                        self.root.after(max(10, ok_msec), _ok_off)
                    except: pass

                ok_bp = inference_cfg.get("ok_buzzer_path", "")
                if ok_bp and PYGAME_AVAILABLE and os.path.exists(ok_bp):
                    try:
                        _ensure_mixer()
                        pygame.mixer.music.load(ok_bp)
                        pygame.mixer.music.play(0)
                    except: pass
            
            elif "SKIP" in results:
                self.update_status(f"SKIP ({pat_name})", COLOR_BG_PANEL)
        
        self.inspecting = False

    def update_status(self, text, color):
        self.lbl_status.config(text=text, fg="white" if color != COLOR_BG_PANEL else COLOR_ACCENT)
        self.header.config(bg=color)
        self.lbl_status.config(bg=color)
        self.lbl_clock.config(bg=color)
        for w in self.header.winfo_children():
            try:
                if not isinstance(w, tk.Button): 
                    w.config(bg=color)
            except: pass

    def add_history(self, trig_id):
        now = datetime.datetime.now()
        t_str = now.strftime("%m/%d %H:%M:%S")
        commit_str = self.get_commit_str()
        self.ng_history.append({"commit": self.commit_number, "commit_str": commit_str, "trigger": trig_id, "time": now})
        self.root.after(0, lambda: self.lb_history.insert(0, f"[{t_str}] #{commit_str} NG"))

    def clear_trigger_queue(self):
        while not self.trigger_queue.empty():
            try:
                self.trigger_queue.get_nowait()
            except queue.Empty:
                break

    def _main_logic_loop(self):
        while self.running:
            try:
                trig_id = self.trigger_queue.get(timeout=1.0)

                if self.settings_open:
                    trig_name = self._get_trig_name(trig_id)
                    self.logger.warning(
                        f"設定画面表示中にトリガーを受信しました（受信={trig_name}）。検査をスキップします。"
                    )
                    continue

                self.process_inspection(trig_id)

                if not self.trigger_queue.empty():
                    cycle_just_completed = (self.cycle_active_pat_id is None and len(self.cycle_fired_trigs) == 0)
                    if cycle_just_completed:
                        self.logger.info("サイクル完了後の余剰トリガーをスキップします")
                        while not self.trigger_queue.empty():
                            try:
                                self.trigger_queue.get_nowait()
                            except queue.Empty:
                                break
            except queue.Empty:
                pass