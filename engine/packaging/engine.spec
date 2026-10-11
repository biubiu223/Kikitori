# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：把 engine_server.py 打包成 onedir 的 KikitoriEngine.exe。

一键打包：powershell -ExecutionPolicy Bypass -File engine\\packaging\\build_engine.ps1（或仓库根目录的 build.ps1）
输出：<仓库>/build/engine/KikitoriEngine/KikitoriEngine.exe + _internal/

要点：
  - console=True（界面用 CreateNoWindow 启动，并重定向 stdin/stdout/stderr）
  - 默认是精简构建：不带 NVIDIA cuBLAS / cuDNN / NVRTC（约 2GB），只带清单 cuda_manifest.json，
    首次用显卡时由引擎下载到 %LOCALAPPDATA%\\Kikitori\\runtime\\cuda12（kikitori/cuda_runtime.py）
  - KIKITORI_BUNDLE_CUDA=1 时做完整构建：DLL 按原来的目录结构放到 _internal/nvidia/<包>/bin
    （作为 datas 收集：不需要 PyInstaller 做依赖分析）
  - ctranslate2 的 DLL 留在 _internal/ctranslate2/（它的 __init__ 会加载自己目录下的所有 *.dll）
  - 引擎没有界面，不打包 tkinter
"""

import glob
import importlib.util
import os

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs, collect_submodules,
                                     copy_metadata)

SPEC_DIR = os.path.abspath(SPECPATH)          # noqa: F821  （PyInstaller 提供）
ENGINE_DIR = os.path.dirname(SPEC_DIR)        # engine/（engine_server.py 和 kikitori 包所在目录）

datas = []
binaries = []
hiddenimports = []

# ---- faster-whisper：assets/silero_vad_v6.onnx ----
datas += collect_data_files("faster_whisper")

# ---- CTranslate2 的 DLL（ctranslate2.dll、cudnn64_9.dll、libiomp5md.dll） ----
binaries += collect_dynamic_libs("ctranslate2")

# ---- 显卡运行库清单：首次用显卡时引擎按它下载 NVIDIA 运行库（docs/protocol.md 第 7.1 节） ----
_manifest = os.path.join(SPEC_DIR, "cuda_manifest.json")
if not os.path.isfile(_manifest):
    raise SystemExit("缺少 engine/packaging/cuda_manifest.json，请先运行 engine/packaging/make_cuda_manifest.py")
datas.append((_manifest, "."))

# ---- nvidia cublas / cudnn / cuda_nvrtc 的 DLL：默认不打包（精简构建）----
# 设置环境变量 KIKITORI_BUNDLE_CUDA=1（build_engine.ps1 -BundleCuda）可以做“完整构建”（开发用）：
# DLL 按原目录结构放到 _internal/nvidia/<包>/bin，cuda_setup.py 会优先用它们，不再下载
if os.environ.get("KIKITORI_BUNDLE_CUDA") == "1":
    _nv = importlib.util.find_spec("nvidia")
    if _nv is None or not _nv.submodule_search_locations:
        raise SystemExit("找不到 nvidia-cublas-cu12 / nvidia-cudnn-cu12，请先在 .venv 里安装 engine/requirements-cuda.txt")
    _nv_count = 0
    for _base in _nv.submodule_search_locations:
        for _bin in sorted(glob.glob(os.path.join(_base, "*", "bin"))):
            _pkg = os.path.basename(os.path.dirname(_bin))
            for _dll in sorted(glob.glob(os.path.join(_bin, "*.dll"))):
                datas.append((_dll, f"nvidia/{_pkg}/bin"))
                _nv_count += 1
    print(f"[engine.spec] 完整构建：收集 nvidia DLL {_nv_count} 个")
else:
    print("[engine.spec] 精简构建：不打包 NVIDIA 运行库（首次用显卡时下载）")

# ---- onnxruntime（faster_whisper.vad 里才 import，静态分析看不到）----
hiddenimports += ["onnxruntime"]
binaries += collect_dynamic_libs("onnxruntime")

# ---- huggingface_hub 用懒加载（__getattr__ + importlib），要把子模块全部带上；hf_xet 是可选的下载加速 ----
hiddenimports += collect_submodules("huggingface_hub", filter=lambda n: not n.startswith("huggingface_hub.cli"))
hiddenimports += ["hf_xet"]

# ---- 引擎自己的模块（engine_server.py 里有些是在函数里才 import 的）----
hiddenimports += [
    "kikitori",
    "kikitori.asr",
    "kikitori.audio",
    "kikitori.cuda_runtime",
    "kikitori.cuda_setup",
    "kikitori.engine",
    "kikitori.languages",
    "kikitori.translator",
    "kikitori.vad",
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
    [os.path.join(ENGINE_DIR, "engine_server.py")],
    pathex=[ENGINE_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "_tkinter",
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
