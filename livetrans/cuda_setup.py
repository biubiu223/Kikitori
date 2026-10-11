"""让 CTranslate2 (faster-whisper) 找到 pip 安装的 cuBLAS / cuDNN DLL。

nvidia-cublas-cu12 / nvidia-cudnn-cu12 会把 DLL 放在 site-packages/nvidia/*/bin，
Windows 默认不会去那里找，所以要在 import faster_whisper 之前把这些目录加进搜索路径。

PyInstaller 打包后（sys.frozen），这些 DLL 按同样的目录结构放在 _internal/nvidia/*/bin
（见 packaging/engine.spec），这里改从 sys._MEIPASS 找。
"""

import os
import sys


def _search_roots() -> list[str]:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
        return [base]

    import site

    roots = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    # 兜底：按 sys.path 里的 site-packages 找（某些虚拟环境里 site.getsitepackages 不全）
    roots.extend(p for p in sys.path if p and os.path.basename(p).lower() == "site-packages")
    return roots


def add_cuda_dll_dirs() -> list[str]:
    if sys.platform != "win32":
        return []

    added = []
    for root in _search_roots():
        nvidia_dir = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia_dir):
            continue
        for name in sorted(os.listdir(nvidia_dir)):
            bin_dir = os.path.normcase(os.path.abspath(os.path.join(nvidia_dir, name, "bin")))
            if os.path.isdir(bin_dir) and bin_dir not in added:
                # add_dll_directory 管直接依赖；cuDNN 内部还会按名字 LoadLibrary 子库，
                # 那种情况只看 PATH，所以两边都加。
                try:
                    os.add_dll_directory(bin_dir)
                except OSError:
                    continue
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                added.append(bin_dir)
    return added
