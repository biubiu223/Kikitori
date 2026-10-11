"""让 CTranslate2 (faster-whisper) 找到 NVIDIA 的 cuBLAS / cuDNN / NVRTC DLL。

查找顺序（docs/protocol.md 第 7.1 节）：
  1. 打包目录自带的：_internal/nvidia/*/bin（只有“完整构建”才带，精简构建不带）
  2. Python 环境里 pip 安装的：site-packages/nvidia/*/bin（源码模式）
  3. 首次使用时下载的运行库：%LOCALAPPDATA%\\Kikitori\\runtime\\cuda12\\（只认已经完整装好的，见 cuda_runtime.py）
     环境变量 KIKITORI_RUNTIME_DIR 可以覆盖这个位置（测试用）

Windows 默认不会去这些目录找 DLL，所以要在用显卡之前把它们加进搜索路径。
注意：CTranslate2 是在第一次用显卡计算时才按名字加载 cublas64_12.dll 的（import 时不需要）；
一旦加载失败，整个进程里的 CTranslate2 显卡功能就坏了（之后的计算会卡死），所以必须先准备好 DLL 再用显卡。

测试用环境变量 KIKITORI_RUNTIME_ONLY=1：忽略 1、2，只用第 3 项（在源码模式下测试“下载运行库”）。
"""

import os
import sys

# 判断一个目录里有没有能用的 CUDA 运行库，看这几个文件
KEY_DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
# 冻结后的 exe 不能用 PATH 里系统装的其它 CUDA：这些 DLL 所在的外部目录会从 PATH 里去掉
_CUDA_DLL_NAMES = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "nvrtc64_120_0.dll")

_added: list[str] = []
_preloaded = []          # 预先按完整路径加载的 DLL（保持引用，防止被卸载）


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def runtime_dir() -> str:
    """首次运行时下载的显卡运行库放在哪里。"""
    d = os.environ.get("KIKITORI_RUNTIME_DIR", "").strip()
    if d:
        return os.path.abspath(d)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Kikitori", "runtime", "cuda12")


def _runtime_only() -> bool:
    return os.environ.get("KIKITORI_RUNTIME_ONLY") == "1"


def _bundled_dirs() -> list[str]:
    """1. 打包目录自带的（完整构建）。"""
    if not frozen() or _runtime_only():
        return []
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    return _nvidia_bins(base)


def _site_dirs() -> list[str]:
    """2. Python 环境里 pip 安装的（源码模式；冻结后不找）。"""
    if frozen() or _runtime_only():
        return []
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
    roots.extend(p for p in sys.path if p and os.path.basename(p).lower() == "site-packages")
    out = []
    for r in roots:
        for d in _nvidia_bins(r):
            if d not in out:
                out.append(d)
    return out


def _nvidia_bins(root: str) -> list[str]:
    nvidia_dir = os.path.join(root, "nvidia")
    if not os.path.isdir(nvidia_dir):
        return []
    out = []
    for name in sorted(os.listdir(nvidia_dir)):
        d = os.path.normcase(os.path.abspath(os.path.join(nvidia_dir, name, "bin")))
        if os.path.isdir(d):
            out.append(d)
    return out


def _has_key_dlls(dirs: list[str]) -> bool:
    return all(any(os.path.isfile(os.path.join(d, n)) for d in dirs) for n in KEY_DLLS)


def find_cuda_dirs() -> tuple[str, list[str]]:
    """按查找顺序找到第一处完整的 CUDA 运行库。返回 (来源, 目录列表)；没有返回 ("", [])。
    来源："bundled" / "python" / "runtime"。"""
    for kind, dirs in (("bundled", _bundled_dirs()), ("python", _site_dirs())):
        if dirs and _has_key_dlls(dirs):
            return kind, dirs
    try:
        from .cuda_runtime import installed_ok
    except ImportError:              # 单独拷出去用的时候
        installed_ok = None
    rd = runtime_dir()
    if installed_ok is not None and installed_ok(rd):
        return "runtime", [os.path.normcase(rd)]
    return "", []


def _sanitize_path_for_frozen(ours: list[str]):
    """冻结后的 exe：把 PATH 里其它地方（例如系统装的 CUDA Toolkit）带 CUDA DLL 的目录去掉，免得混用版本。"""
    if not frozen():
        return
    keep = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if not p:
            continue
        np_ = os.path.normcase(os.path.abspath(p))
        if np_ not in ours and any(os.path.isfile(os.path.join(p, n)) for n in _CUDA_DLL_NAMES):
            print(f"[cuda] 忽略 PATH 里的外部 CUDA 目录：{p}", file=sys.stderr)
            continue
        keep.append(p)
    os.environ["PATH"] = os.pathsep.join(keep)


def add_cuda_dll_dirs() -> list[str]:
    """把找到的 CUDA DLL 目录加进 DLL 搜索路径（可以重复调用：下载完运行库后再调一次）。返回已加入的目录。"""
    if sys.platform != "win32":
        return []
    kind, dirs = find_cuda_dirs()
    _sanitize_path_for_frozen(dirs)
    for d in dirs:
        if d in _added:
            continue
        # add_dll_directory 管直接依赖；cuDNN 内部还会按名字 LoadLibrary 子库，
        # 那种情况只看 PATH，所以两边都加。
        try:
            os.add_dll_directory(d)
        except OSError:
            continue
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        _added.append(d)
    return list(_added)


def preload_cuda_dlls() -> str | None:
    """按完整路径预先加载 cuBLAS。成功返回 None，失败返回原因。

    这样 CTranslate2 之后按名字加载时直接用进程里已经加载的这一份（不会去别处找别的版本），
    而且能在真正用显卡之前发现 DLL 损坏/缺失——CTranslate2 自己加载失败后整个进程的显卡功能就坏了。
    """
    import ctypes

    if _preloaded:
        return None
    kind, dirs = find_cuda_dirs()
    if not dirs:
        return "找不到显卡运行库（cublas64_12.dll）"
    for name in KEY_DLLS:
        path = next((os.path.join(d, name) for d in dirs if os.path.isfile(os.path.join(d, name))), None)
        if path is None:
            return f"找不到 {name}"
        try:
            _preloaded.append(ctypes.WinDLL(path))
        except OSError as e:
            return f"{name} 无法加载：{e}"
    return None
