# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把 engine_server.py 打包成 onedir 的 KikitoriEngine.exe。

一键打包：powershell -ExecutionPolicy Bypass -File packaging\\build_engine.ps1
输出：build/engine/KikitoriEngine/KikitoriEngine.exe + _internal/

要点：
  - console=True（界面用 CreateNoWindow 启动，并重定向 stdin/stdout/stderr）
  - nvidia cuBLAS / cuDNN / NVRTC 的 DLL 按原来的目录结构放到 _internal/nvidia/<包>/bin，
    livetrans/cuda_setup.py 在冻结模式下从 sys._MEIPASS 找它们
    （作为 datas 收集：这些 DLL 有 2GB，不需要 PyInstaller 做依赖分析）
  - ctranslate2 的 DLL 留在 _internal/ctranslate2/（它的 __init__ 会加载自己目录下的所有 *.dll）
  - 不打包 tkinter（悬浮字幕版 overlay.py 不属于引擎进程）
"""

import glob
import importlib.util
import os

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs, collect_submodules,
                                     copy_metadata)

SPEC_DIR = os.path.abspath(SPECPATH)          # noqa: F821  （PyInstaller 提供）
ROOT = os.path.dirname(SPEC_DIR)              # 仓库根目录（engine_server.py 所在目录）

datas = []
binaries = []
hiddenimports = []

# ---- faster-whisper：assets/silero_vad_v6.onnx ----
datas += collect_data_files("faster_whisper")

# ---- CTranslate2 的 DLL（ctranslate2.dll、cudnn64_9.dll、libiomp5md.dll） ----
binaries += collect_dynamic_libs("ctranslate2")

# ---- nvidia cublas / cudnn / cuda_nvrtc 的 bin 目录 ----
_nv = importlib.util.find_spec("nvidia")
if _nv is None or not _nv.submodule_search_locations:
    raise SystemExit("找不到 nvidia-cublas-cu12 / nvidia-cudnn-cu12，请先在 .venv 里安装 requirements.txt")
_nv_count = 0
for _base in _nv.submodule_search_locations:
    for _bin in sorted(glob.glob(os.path.join(_base, "*", "bin"))):
        _pkg = os.path.basename(os.path.dirname(_bin))
        for _dll in sorted(glob.glob(os.path.join(_bin, "*.dll"))):
            datas.append((_dll, f"nvidia/{_pkg}/bin"))
            _nv_count += 1
if _nv_count == 0:
    raise SystemExit("nvidia/*/bin 里没有找到 DLL")
print(f"[engine.spec] 收集 nvidia DLL {_nv_count} 个")

# ---- onnxruntime（faster_whisper.vad 里才 import，静态分析看不到）----
hiddenimports += ["onnxruntime"]
binaries += collect_dynamic_libs("onnxruntime")

# ---- huggingface_hub 用懒加载（__getattr__ + importlib），要把子模块全部带上；hf_xet 是可选的下载加速 ----
hiddenimports += collect_submodules("huggingface_hub", filter=lambda n: not n.startswith("huggingface_hub.cli"))
hiddenimports += ["hf_xet"]

# ---- 引擎用到的 livetrans 模块（不含 overlay.py，避免把 tkinter 带进来） ----
hiddenimports += [
    "livetrans",
    "livetrans.asr",
    "livetrans.audio",
    "livetrans.cuda_setup",
    "livetrans.engine",
    "livetrans.languages",
    "livetrans.translator",
    "livetrans.vad",
]

# ---- 运行时会用 importlib.metadata 查版本/是否安装的包 ----
for _dist in ("huggingface_hub", "hf_xet", "tqdm", "tokenizers", "faster_whisper", "ctranslate2",
              "onnxruntime", "numpy", "httpx", "httpcore", "filelock", "packaging", "fsspec", "PyYAML",
              "av", "soxr", "PyAudioWPatch"):
    try:
        datas += copy_metadata(_dist)
    except Exception as _e:
        print(f"[engine.spec] 跳过 {_dist} 的 metadata：{_e}")

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "engine_server.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "_tkinter", "livetrans.overlay",
        "torch", "tensorflow", "transformers", "IPython", "matplotlib", "pandas", "scipy",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KikitoriEngine",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="KikitoriEngine",
)
