# ==========================================
# 1. 警告・不要なサードパーティログの超強力な抑制 & VRAM対策設定
# (ライブラリのインポート前に実行する必要があります)
# ==========================================
import os
import sys
import logging
import warnings
import inspect  # 引数確認用の標準ライブラリ
import json     # JSON出力用

# GPUメモリ（VRAM）の断片化を防ぎ、効率的に利用するための環境設定
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

# 標準の警告出力を完全に無視 (FutureWarning, DeprecationWarning, UserWarning等)
warnings.filterwarnings("ignore")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# Lightningや各ライブラリのコンソールログを「エラーのみ」に制限
os.environ["LIGHTNING_EXCLUDE_SELF_MONITORING"] = "1"
os.environ["LIGHTNING_LOG_LEVEL"] = "ERROR"
logging.getLogger("lightning.pytorch").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)
logging.getLogger("anomalib").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("requests").setLevel(logging.ERROR)
logging.getLogger("timm").setLevel(logging.ERROR)

try:
    from lightning.pytorch.utilities import disable_possible_user_warnings
    disable_possible_user_warnings()
except ImportError:
    pass

# ==========================================
# 2. 必要なライブラリのインポート
# ==========================================
from pathlib import Path
import tkinter as tk
from tkinter import filedialog
from tkinter import ttk  # ドロップダウンメニュー(Combobox)用
import pandas as pd
import torch
from anomalib.data import Folder
from anomalib.models import Patchcore
from anomalib.engine import Engine

# Lightningのコールバッククラスを安全にインポート
try:
    from lightning.pytorch.callbacks import Callback
except ImportError:
    from pytorch_lightning.callbacks import Callback

# Tensor Coresの活用 (RTX 3060 Ti等)
torch.set_float32_matmul_precision("medium")

# ==========================================
# 【設定項目】 判定感度の調整 (しきい値)
# ==========================================
# Anomalibのデフォルト判定基準値は 0.5 です。
# 0.49以上のスコアを持つ画像を Abnormal と判定します。
CLASSIFICATION_THRESHOLD = 0.49


# ==========================================
# 可視化漏れを防ぐためのリアルタイム割り込みコールバック
# ==========================================
class ThresholdAdjustmentCallback(Callback):
    """
    推論直後、可視化エンジンが実行される直前にバッチデータをキャッチし、
    カスタムしきい値(0.49)以上のアノマリスコアを持つデータの予測ラベルを
    強制的に「異常(1)」に更新することで、可視化・画像保存を確実に行わせるコールバック。
    """
    def __init__(self, threshold=0.49):
        super().__init__()
        self.threshold = threshold

    def _adjust_batch(self, outputs):
        if outputs is None:
            return
            
        scores = None
        for s_attr in ["pred_scores", "pred_score", "anomaly_scores", "scores"]:
            if hasattr(outputs, s_attr):
                scores = getattr(outputs, s_attr)
                break
        if scores is None and isinstance(outputs, dict):
            scores = outputs.get("pred_scores") or outputs.get("pred_score") or outputs.get("anomaly_scores")

        if scores is not None:
            # デバイス(CPU/GPU)を維持したまま、しきい値以上のスコアを「1（異常）」にするテンソルを作成
            new_labels = (scores >= self.threshold).long()
            
            # オブジェクト属性の更新
            for l_attr in ["pred_labels", "pred_label", "labels"]:
                if hasattr(outputs, l_attr):
                    setattr(outputs, l_attr, new_labels)
            
            # 辞書キーの更新
            if isinstance(outputs, dict):
                for l_key in ["pred_labels", "pred_label", "labels"]:
                    if l_key in outputs:
                        outputs[l_key] = new_labels
                outputs["pred_labels"] = new_labels

    def on_test_batch_end(self, trainer, pl_module, outputs, batch, batch_idx, dataloader_idx=0):
        try:
            self._adjust_batch(outputs)
        except Exception:
            pass

    def on_predict_batch_end(self, trainer, pl_module, outputs, batch, batch_idx, dataloader_idx=0):
        try:
            self._adjust_batch(outputs)
        except Exception:
            pass


def select_dataset_root() -> Path:
    """
    GUIのフォルダ選択ウィンドウを表示し、ユーザーにデータセットフォルダを指定させる
    """
    root_window = tk.Tk()
    root_window.withdraw()
    root_window.attributes('-topmost', True)
    
    selected_path = filedialog.askdirectory(
        title="データセットフォルダ（例: door_coupler）を選択してください"
    )
    
    root_window.destroy()
    return Path(selected_path).resolve() if selected_path else None

def show_parameter_dialog() -> tuple[tuple[int, int], int]:
    """
    学習開始前に画像解像度とバッチサイズを選択するためのダイアログボックスを表示する。
    """
    root = tk.Tk()
    root.title("PatchCore パラメータ設定")
    root.geometry("400x220")
    root.resizable(False, False)
    root.attributes('-topmost', True)  # 最前面に表示

    selected_values = {"resolution": (512, 512), "batch_size": 4}

    frame = ttk.Frame(root, padding=20)
    frame.pack(fill=tk.BOTH, expand=True)

    # 1. 解像度のドロップダウン
    ttk.Label(frame, text="画像解像度 (IMAGE_RESOLUTION):", font=("", 10)).grid(row=0, column=0, sticky=tk.W, pady=12)
    res_options = ["256x256", "384x384", "512x512", "768x768", "1024x1024"]
    res_combo = ttk.Combobox(frame, values=res_options, state="readonly", width=15, font=("", 10))
    res_combo.set("512x512")
    res_combo.grid(row=0, column=1, pady=12, padx=10, sticky=tk.E)

    # 2. バッチサイズのドロップダウン
    ttk.Label(frame, text="バッチサイズ (BATCH_SIZE):", font=("", 10)).grid(row=1, column=0, sticky=tk.W, pady=12)
    batch_options = ["1", "2", "4", "8", "16", "32"]
    batch_combo = ttk.Combobox(frame, values=batch_options, state="readonly", width=15, font=("", 10))
    batch_combo.set("4")
    batch_combo.grid(row=1, column=1, pady=12, padx=10, sticky=tk.E)

    def on_submit():
        res_str = res_combo.get()
        w, h = map(int, res_str.split("x"))
        selected_values["resolution"] = (w, h)
        selected_values["batch_size"] = int(batch_combo.get())
        root.destroy()

    submit_btn = ttk.Button(frame, text="学習開始 (Start Training)", command=on_submit)
    submit_btn.grid(row=2, column=0, columnspan=2, pady=15)

    def on_closing():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()

    return selected_values["resolution"], selected_values["batch_size"]

def calculate_dynamic_coreset_ratio(train_dir: Path) -> float:
    """
    トレーニング画像の枚数に基づき、コアセット・サンプリング比率を動的に計算する
    """
    train_images = list(train_dir.glob("**/*.png")) + list(train_dir.glob("**/*.jpg")) + list(train_dir.glob("**/*.bmp"))
    num_train_images = len(train_images)
    print(f"[自動検出] 学習用正常画像の枚数: {num_train_images} 枚")

    if num_train_images == 0:
        return 0.1

    estimated_patches_per_image = 500
    total_estimated_patches = num_train_images * estimated_patches_per_image
    target_memory_size = 8000

    dynamic_ratio = target_memory_size / total_estimated_patches
    ratio = max(0.02, min(0.45, dynamic_ratio))
    ratio = round(ratio, 3)

    return ratio

def collect_predictions(engine, model, datamodule, phase_name, results_list, classification_threshold=0.5):
    """
    指定されたデータモジュールに対して推論を行い、予測結果(スコア・判定ラベル)を回収するヘルパー関数
    """
    predictions = engine.predict(model=model, datamodule=datamodule)

    for batch in predictions:
        scores = None
        paths = None

        for s_attr in ["pred_scores", "pred_score", "anomaly_scores", "scores"]:
            if hasattr(batch, s_attr):
                scores = getattr(batch, s_attr)
                break
        for p_attr in ["image_path", "image_paths", "path", "paths"]:
            if hasattr(batch, p_attr):
                paths = getattr(batch, p_attr)
                break

        if scores is None and isinstance(batch, dict):
            scores = batch.get("pred_scores") or batch.get("pred_score")
        if paths is None and isinstance(batch, dict):
            paths = batch.get("image_path") or batch.get("image_paths")

        if scores is not None:
            length = len(scores) if hasattr(scores, "__len__") else 1
            for i in range(length):
                try:
                    score = float(scores[i].item() if hasattr(scores[i], "item") else scores[i])
                except Exception:
                    score = 0.0

                path = str(paths[i]) if paths is not None and i < len(paths) else "unknown"
                
                # --- 正解ラベルの割り当てロジック (test/good, train/good は実際は Normal、その他は Abnormal) ---
                path_normalized = path.replace("\\", "/").lower()
                if "test/good" in path_normalized or "train/good" in path_normalized:
                    actual_label = "Normal"     # 実際は「正（正常）」
                else:
                    actual_label = "Abnormal"   # 実際は「誤（異常）」

                # 設定されたしきい値に基づいて判定ラベルを決定
                pred_label = "Abnormal" if score >= classification_threshold else "Normal"
                
                # 判定が正しかったかどうか (正解: Yes / 不正解: No)
                is_correct = "Yes" if pred_label == actual_label else "No"
                
                results_list.append({
                    "Phase": phase_name.upper(),
                    "Image_Path": path,
                    "Anomaly_Score": score,
                    "Actual_Label": actual_label,
                    "Pred_Label": pred_label,
                    "Is_Correct": is_correct
                })

def main():
    # 1. ユーザーに絶対パスでデータセットフォルダを選ばせる
    dataset_root = select_dataset_root()
    
    if not dataset_root or not dataset_root.exists():
        print("エラー: フォルダが選択されなかったか、存在しません。処理を中断します。")
        return

    print(f"対象フォルダ: {dataset_root}")

    # 2. パラメータ設定ダイアログを表示
    image_resolution, batch_size = show_parameter_dialog()
    print(f"[設定確認] 解像度: {image_resolution[0]}x{image_resolution[1]}, バッチサイズ: {batch_size}")

    # 3. 各ディレクトリの絶対パスを直接定義
    train_good_path = dataset_root / "train" / "good"
    test_path = dataset_root / "test"

    if not train_good_path.exists():
        print(f"エラー: 'train/good' が見つかりません: {train_good_path}")
        return
    if not test_path.exists():
        print(f"エラー: 'test' が見つかりません: {test_path}")
        return

    # テストデータのフォルダ構成確認
    test_good_path = test_path / "good"
    abnormal_dirs = [p for p in test_path.iterdir() if p.is_dir() and p.name != "good"]
    
    if not test_good_path.exists():
        print("警告: 'test/good' フォルダが見つかりませんでした。")
        test_good_dir_str = None
    else:
        test_good_dir_str = str(test_good_path)
        
    if not abnormal_dirs:
        print("警告: 'test' 内に異常検出用のサブフォルダが見つかりませんでした。")
        abnormal_dir_str = None
    elif len(abnormal_dirs) == 1:
        abnormal_dir_str = str(abnormal_dirs[0])
    else:
        abnormal_dir_str = [str(p) for p in abnormal_dirs]

    # 4. 動的コアセット比率の計算
    optimal_ratio = calculate_dynamic_coreset_ratio(train_good_path)

    # 5. データモジュールの構築 (テストデータの全量確保と検証分割の防止)
    dataset_name = dataset_root.name

    datamodule = Folder(
        name=dataset_name,
        root=None,  
        normal_dir=str(train_good_path),
        normal_test_dir=test_good_dir_str,  
        abnormal_dir=abnormal_dir_str,
        normal_split_ratio=0.0,             # 正常データの自動分割を禁止
        test_split_mode="from_dir",          # ディレクトリ構造からテストデータを読み込む
        test_split_ratio=0.0,               # 自動でのテスト分割を無効化
        val_split_mode="same_as_test",      # テスト用のgoodをモデル検証にもコピーして流用
        val_split_ratio=0.0,                # 検証用に画像を抜き取る比率をゼロに設定
        train_batch_size=batch_size,        
        eval_batch_size=batch_size,         
        num_workers=4                       # ワーカー並行数を下げてVRAMオーバーヘッドを抑制
    )

    # 6. 学習画像(train/good)を評価・判定するための専用推論データモジュール
    train_predict_datamodule = Folder(
        name=dataset_name + "_train_predict",
        root=None,
        normal_dir=str(train_good_path),
        normal_test_dir=str(train_good_path),  # train/goodをテストの正常用フォルダとして流用
        abnormal_dir=None,                     
        normal_split_ratio=0.0,
        test_split_mode="from_dir",
        test_split_ratio=0.0,
        val_split_mode="same_as_test",
        val_split_ratio=0.0,
        train_batch_size=batch_size,
        eval_batch_size=batch_size,
        num_workers=4
    )

    # 7. 【解像度制御】前処理エンジン（pre_processor）の構築
    pre_processor = None
    try:
        try:
            pre_processor = Patchcore.configure_pre_processor(
                image_size=image_resolution,
                center_crop_size=image_resolution
            )
        except TypeError:
            try:
                pre_processor = Patchcore.configure_pre_processor(
                    image_size=image_resolution,
                    crop_size=image_resolution
                )
            except TypeError:
                pre_processor = Patchcore.configure_pre_processor(
                    image_size=image_resolution
                )
    except Exception as e:
        print(f"警告: 前処理の解像度設定中にエラーが発生したため、デフォルトで初期化します: {e}")

    # 8. 画像可視化エンジンの段階的な読み込み (エラー対策)
    visualizer = None
    try:
        from anomalib.visualization import ImageVisualizer
        try:
            visualizer = ImageVisualizer()
        except TypeError:
            try:
                visualizer = ImageVisualizer(mode="full")
            except TypeError:
                visualizer = ImageVisualizer(save_images=True)
    except ImportError:
        try:
            from anomalib.utils.callbacks import VisualizerCallback
            visualizer = VisualizerCallback(task="classification", mode="full", save_images=True)
        except ImportError:
            pass

    # 9. モデルの構築 (精度の高い wide_resnet50_2 を指定)
    model_kwargs = {
        "backbone": "wide_resnet50_2",
        "coreset_sampling_ratio": optimal_ratio,
    }
    
    # バージョンごとにモデルが pre_processor / visualizer 引数を受け付けるか判定してセット
    patchcore_sig = inspect.signature(Patchcore.__init__)
    if "pre_processor" in patchcore_sig.parameters and pre_processor is not None:
        model_kwargs["pre_processor"] = pre_processor
    
    if "visualizer" in patchcore_sig.parameters and visualizer is not None:
        model_kwargs["visualizer"] = visualizer

    model = Patchcore(**model_kwargs)

    # 保険：古いバージョン対策
    if pre_processor is not None:
        if not hasattr(model, "pre_processor") or getattr(model, "pre_processor") is None:
            model.pre_processor = pre_processor

    # 10. 学習の実行
    print(f"モデルの学習・特徴抽出を実行中... (解像度: {image_resolution[0]}x{image_resolution[1]}, バッチサイズ: {batch_size})")
    
    # 古いCUDAメモリを強制的に解放
    torch.cuda.empty_cache()
    
    # しきい値調整コールバックを最優先（リストの先頭）で作成・追加
    threshold_callback = ThresholdAdjustmentCallback(threshold=CLASSIFICATION_THRESHOLD)
    callbacks = [threshold_callback]
    
    if visualizer is not None:
        if not hasattr(model, "visualizer") or getattr(model, "visualizer") is None:
            callbacks.append(visualizer)

    engine = Engine(max_epochs=1, logger=False, enable_progress_bar=False, callbacks=callbacks)
    engine.fit(model=model, datamodule=datamodule)

    # 11. 各フェーズの判定結果を回収
    results_list = []
    
    # 11-1. テストデータセットを用いてしきい値を決定 & テストデータの予測結果を取得
    print("テストデータ (Test Data) の全画像判定とヒートマップ画像出力を実行中...")
    engine.test(model=model, datamodule=datamodule, verbose=False)
    
    # カスタムしきい値(CLASSIFICATION_THRESHOLD)を適用して結果を回収
    collect_predictions(engine, model, datamodule, "test", results_list, CLASSIFICATION_THRESHOLD)

    # 11-2. 学習用正常データの予測結果を取得
    print("学習用正常データ (Train Data) の全画像判定とヒートマップ画像出力を実行中...")
    collect_predictions(engine, model, train_predict_datamodule, "train", results_list, CLASSIFICATION_THRESHOLD)

    # 12. 結果出力フォルダの取得と保存
    try:
        log_dir = Path(engine.trainer.default_root_dir)
    except Exception:
        log_dir = Path("./results")
    
    log_dir.mkdir(parents=True, exist_ok=True)
    output_excel_path = log_dir / "all_inspection_results.xlsx"
    output_json_path = log_dir / "optimal_settings.json"

    # --- 13. 評価指標（メトリクス）の算出 ---
    df = pd.DataFrame(results_list)
    
    total_samples = len(df)
    correct_predictions = len(df[df["Is_Correct"] == "Yes"])
    accuracy = correct_predictions / total_samples if total_samples > 0 else 0.0

    # 13-1. 【異常(Abnormal)を陽性(Positive)とした場合の各件数計算】
    tp_abnormal = len(df[(df["Actual_Label"] == "Abnormal") & (df["Pred_Label"] == "Abnormal")])
    fp_abnormal = len(df[(df["Actual_Label"] == "Normal") & (df["Pred_Label"] == "Abnormal")])
    fn_abnormal = len(df[(df["Actual_Label"] == "Abnormal") & (df["Pred_Label"] == "Normal")])
    tn_abnormal = len(df[(df["Actual_Label"] == "Normal") & (df["Pred_Label"] == "Normal")])

    precision_abnormal = tp_abnormal / (tp_abnormal + fp_abnormal) if (tp_abnormal + fp_abnormal) > 0 else 0.0
    recall_abnormal = tp_abnormal / (tp_abnormal + fn_abnormal) if (tp_abnormal + fn_abnormal) > 0 else 0.0
    f1_abnormal = 2 * (precision_abnormal * recall_abnormal) / (precision_abnormal + recall_abnormal) if (precision_abnormal + recall_abnormal) > 0 else 0.0

    # 13-2. 【正常(Normal)を陽性(Positive)とした場合の計算】
    tp_normal = tn_abnormal
    fp_normal = fn_abnormal
    fn_normal = fp_abnormal
    tn_normal = tp_abnormal

    precision_normal = tp_normal / (tp_normal + fp_normal) if (tp_normal + fp_normal) > 0 else 0.0
    recall_normal = tp_normal / (tp_normal + fn_normal) if (tp_normal + fn_normal) > 0 else 0.0
    f1_normal = 2 * (precision_normal * recall_normal) / (precision_normal + recall_normal) if (precision_normal + recall_normal) > 0 else 0.0

    # 指標データフレーム
    summary_data = {
        "評価対象 (Target Class)": [
            "全体 (Overall)",
            "異常 (Abnormal)",
            "異常 (Abnormal)",
            "異常 (Abnormal)",
            "正常 (Normal)",
            "正常 (Normal)",
            "正常 (Normal)"
        ],
        "評価指標 (Evaluation Metric)": [
            "全体正解率 (Accuracy)",
            "不具合検出適合率 (Precision)",
            "不具合検出再現率 (Recall / カバー率)",
            "不具合判定 F1値 (F1-score)",
            "正常見極め適合率 (Precision)",
            "正常見極め再現率 (Recall / カバー率)",
            "正常判定 F1値 (F1-score)"
        ],
        "算出結果 (Score)": [
            accuracy,
            precision_abnormal,
            recall_abnormal,
            f1_abnormal,
            precision_normal,
            recall_normal,
            f1_normal
        ]
    }
    summary_df = pd.DataFrame(summary_data)

    # 13-3. 【混同行列 (Confusion Matrix) データフレームの作成】
    confusion_matrix_data = {
        "実際 \\ 予測 (Actual \\ Predicted)": [
            "実際: 正常 (Actual Normal)",
            "実際: 異常 (Actual Abnormal)"
        ],
        "予測: 正常 (Predicted Normal)": [tn_abnormal, fn_abnormal],
        "予測: 異常 (Predicted Abnormal)": [fp_abnormal, tp_abnormal]
    }
    confusion_matrix_df = pd.DataFrame(confusion_matrix_data)

    # 13-4. エクセルを複数シート形式で保存
    with pd.ExcelWriter(output_excel_path, engine="openpyxl") as writer:
        # シート1: 詳細な判定結果
        df.to_excel(writer, index=False, sheet_name="Inspection Results")
        
        # シート2: 評価指標の要約 (全体正解率や適合率)
        summary_df.to_excel(writer, index=False, sheet_name="Evaluation Summary", startrow=0)
        
        # 混同行列用の見出しを空行を挟んで A10 (row index 9) に配置
        title_df = pd.DataFrame([["■ 混同行列 (Confusion Matrix)"]])
        title_df.to_excel(writer, index=False, header=False, sheet_name="Evaluation Summary", startrow=9)
        
        # 混同行列の本体を A12 (row index 11) から配置
        confusion_matrix_df.to_excel(writer, index=False, sheet_name="Evaluation Summary", startrow=11)

    # 13-5. 推論プログラム用最適パラメータを JSON 出力
    raw_threshold = None
    try:
        if hasattr(model, "image_threshold") and hasattr(model.image_threshold, "value"):
            raw_threshold = float(model.image_threshold.value.item())
    except Exception:
        pass

    settings_dict = {
        "dataset_name": dataset_name,
        "backbone": "wide_resnet50_2",
        "coreset_sampling_ratio": optimal_ratio,
        "classification_threshold_normalized": CLASSIFICATION_THRESHOLD,  # 正規化スコアに対する実務上のしきい値
        "raw_image_threshold": raw_threshold,  
        "input_image_resolution": image_resolution  
    }

    with open(output_json_path, "w", encoding="utf-8") as json_file:
        json.dump(settings_dict, json_file, indent=4, ensure_ascii=False)
    
    print("\n" + "="*60)
    print(f"✔ 判定および各種ファイル出力が完了しました！")
    print(f"📄 判定結果Excel  : {output_excel_path.resolve()} (複数シート形式)")
    print(f"⚙ 判定用設定JSON  : {output_json_path.resolve()}")
    print(f"🖼 判定画像フォルダ: '{log_dir.resolve()}' 配下など")
    print("="*60)

if __name__ == "__main__":
    main()