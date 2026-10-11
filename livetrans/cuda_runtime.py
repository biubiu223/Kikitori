"""首次使用显卡时下载 NVIDIA 运行库（cuBLAS / cuDNN / NVRTC 的 DLL）。规范见 docs/protocol.md 第 7.1 节。

  - 按固定版本的清单（packaging/cuda_manifest.json）下载 NVIDIA 在 PyPI 上发布的官方 wheel，逐个校验 sha256
  - 只解压 wheel 里的 nvidia/*/bin/*.dll，平铺到运行库目录（默认 %LOCALAPPDATA%\\Kikitori\\runtime\\cuda12\\），然后删掉 wheel
  - 全部装好后写标记文件 kikitori-runtime.json，记录清单的哈希；清单变了（升级 CUDA）就重新下载
  - 下载中的文件存成 .part，下次用 HTTP Range 断点续传；换下载源也能接着下（同一个文件，sha256 兜底）
  - 下载源：official = PyPI 官方；china = 清华、阿里云镜像（路径和 PyPI 一样是 packages/...）；
    auto = 先同时测各个源的速度，按快慢排序，一个源失败自动换下一个
  - cancel() 可以从别的线程中止（stop / shutdown），已下载的部分保留

测试用环境变量：
  KIKITORI_DOWNLOAD_MIRRORS  用 JSON 列表替换下载源，例如 [{"name":"本地","base":"http://127.0.0.1:8767/ok/","group":"china"}]
"""

import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor

from .cuda_setup import runtime_dir

MARKER = "kikitori-runtime.json"
DOWNLOADS = "_downloads"

SOURCES = [
    {"name": "PyPI 官方", "base": "https://files.pythonhosted.org/", "group": "official"},
    {"name": "清华镜像", "base": "https://pypi.tuna.tsinghua.edu.cn/", "group": "china"},
    {"name": "阿里云镜像", "base": "https://mirrors.aliyun.com/pypi/", "group": "china"},
]

SPEED_TEST_BYTES = 2 << 20     # 测速：每个源最多下 2MB
SPEED_TEST_SECONDS = 4.0       # 或者最多 4 秒
PROGRESS_INTERVAL = 0.5        # 进度最多每 0.5 秒报一次

HINT = "可以在 设置 → 语音识别 里切换下载源后重试，或者改用 CPU"


class Cancelled(Exception):
    """下载被 stop / shutdown 中止。"""


class InstallError(Exception):
    """下载/安装失败，str(e) 是给用户看的完整提示。"""


class _SourceError(Exception):
    """这个下载源不行（连不上、超时、HTTP 错误、文件不对），可以换下一个源再试。"""


# ======================================================================
#  清单 / 安装状态
# ======================================================================
def manifest_path() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
        return os.path.join(base, "cuda_manifest.json")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging",
                        "cuda_manifest.json")


def load_manifest() -> tuple[dict, str]:
    """返回 (清单, 清单哈希)。哈希只算 packages 部分（规范化后的 JSON），和文件格式/注释无关。"""
    with open(manifest_path(), encoding="utf-8") as f:
        manifest = json.load(f)
    canon = json.dumps(manifest["packages"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return manifest, hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _expected_dlls(manifest: dict) -> dict[str, int]:
    out = {}
    for p in manifest["packages"]:
        out.update(p.get("dlls", {}))
    return out


def installed_ok(dest: str | None = None) -> bool:
    """运行库目录是否已经完整装好（标记文件的清单哈希和当前清单一致，DLL 都在而且大小对）。"""
    dest = dest or runtime_dir()
    try:
        with open(os.path.join(dest, MARKER), encoding="utf-8") as f:
            marker = json.load(f)
        manifest, mhash = load_manifest()
    except (OSError, ValueError, KeyError):
        return False
    if marker.get("manifest_sha256") != mhash:
        return False
    for name, size in _expected_dlls(manifest).items():
        p = os.path.join(dest, name)
        if not os.path.isfile(p) or os.path.getsize(p) != size:
            return False
    return True


def has_nvidia_driver() -> bool:
    """有没有 NVIDIA 显卡驱动（nvcuda.dll 是驱动自带的 CUDA 驱动接口，没装驱动就没有）。"""
    if sys.platform != "win32":
        return False
    sysroot = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.isfile(os.path.join(sysroot, "System32", "nvcuda.dll"))


def sources_for(mode: str) -> list[dict]:
    srcs = SOURCES
    override = os.environ.get("KIKITORI_DOWNLOAD_MIRRORS", "").strip()
    if override:
        srcs = json.loads(override)
    if mode == "official":
        return [s for s in srcs if s.get("group") == "official"] or list(srcs)
    if mode == "china":
        return [s for s in srcs if s.get("group") == "china"] or list(srcs)
    return list(srcs)


def _describe_error(e: Exception) -> str:
    import httpx

    if isinstance(e, _SourceError):
        return str(e)
    if isinstance(e, httpx.TimeoutException):
        return "连接超时"
    if isinstance(e, httpx.ConnectError):
        return "连不上下载服务器"
    if isinstance(e, httpx.HTTPStatusError):
        return f"服务器返回错误 {e.response.status_code}"
    if isinstance(e, httpx.TransportError):
        return f"网络中断（{type(e).__name__}）"
    return f"{type(e).__name__}: {e}"


def _gb(n: float) -> str:
    return f"{n / (1 << 30):.1f} GB"


# ======================================================================
#  下载 + 安装
# ======================================================================
class RuntimeInstaller:
    """在调用线程里同步执行 run()；别的线程可以随时 cancel()。"""

    def __init__(self, source_mode: str = "auto", on_progress=None, dest: str | None = None):
        import httpx

        self.mode = source_mode if source_mode in ("auto", "official", "china") else "auto"
        self.on_progress = on_progress or (lambda done, total, source: None)
        self.dest = os.path.abspath(dest or runtime_dir())
        self.dl_dir = os.path.join(self.dest, DOWNLOADS)
        self.source_used = ""
        self._cancel = threading.Event()
        self._resp_lock = threading.Lock()
        self._resp = None
        self._client = httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True,
                                    headers={"User-Agent": "Kikitori-engine"})
        self._last_report = 0.0

    # ------------------------------------------------------------------
    def cancel(self):
        self._cancel.set()
        with self._resp_lock:
            r = self._resp
        if r is not None:
            try:
                r.close()          # 让正在阻塞读取的线程马上出来
            except Exception:
                pass

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def _report(self, done: int, total: int, source: str, force: bool = False):
        now = time.monotonic()
        if not force and now - self._last_report < PROGRESS_INTERVAL:
            return
        self._last_report = now
        try:
            self.on_progress(done, total, source)
        except Exception:
            traceback.print_exc()

    # ------------------------------------------------------------------
    def run(self) -> str:
        """下载并安装。成功返回用到的下载源名字；被取消抛 Cancelled；失败抛 InstallError。"""
        try:
            return self._run()
        except (Cancelled, InstallError):
            raise
        except Exception as e:
            if self._cancel.is_set():
                raise Cancelled()
            traceback.print_exc()
            raise InstallError(f"显卡运行库安装失败：{e}。{HINT}")

    def _run(self) -> str:
        try:
            manifest, mhash = load_manifest()
        except Exception as e:
            raise InstallError(f"显卡运行库清单损坏或缺失（{manifest_path()}）：{e}。请重新安装本程序，或者改用 CPU")
        pkgs = manifest["packages"]
        total = sum(p["size"] for p in pkgs)
        try:
            os.makedirs(self.dl_dir, exist_ok=True)
        except OSError as e:
            raise InstallError(f"无法创建运行库目录 {self.dest}：{e}")

        todo = [p for p in pkgs if not self._wheel_installed(p)]
        done_bytes = total - sum(p["size"] for p in todo)
        print(f"[runtime] 运行库目录 {self.dest}；需要下载 {len(todo)}/{len(pkgs)} 个包"
              f"（{sum(p['size'] for p in todo) / 2**20:.0f} MB），下载源模式 {self.mode}", file=sys.stderr)
        self._check_disk(todo)

        sources = self._rank_sources(pkgs) if todo else []
        idx = 0
        tried: list[str] = []
        for p in todo:
            self._check_cancel()
            while True:
                if idx >= len(sources):
                    names = "、".join(dict.fromkeys(tried)) or "无"
                    raise InstallError(f"显卡运行库下载失败：{self._last_error}（已尝试：{names}）。{HINT}")
                src = sources[idx]
                tried.append(src["name"])
                try:
                    path = self._download_verified(src, p, done_bytes, total)
                    break
                except _SourceError as e:
                    self._last_error = str(e)
                    print(f"[runtime] {src['name']} 下载 {p['filename']} 失败：{e}，换下一个源", file=sys.stderr)
                    idx += 1
            self.source_used = src["name"]
            self._extract(p, path)
            done_bytes += p["size"]
            self._report(done_bytes, total, src["name"], force=True)

        self._finish(manifest, mhash)
        return self.source_used or "本地已有"

    _last_error = "未知错误"

    # ------------------------------------------------------------------
    def _done_marker(self, p: dict) -> str:
        return os.path.join(self.dl_dir, p["filename"] + ".done")

    def _wheel_installed(self, p: dict) -> bool:
        """这个包是不是已经解压好了（上次装到一半中断时，装好的包不用重下）。"""
        try:
            with open(self._done_marker(p), encoding="utf-8") as f:
                if f.read().strip() != p["sha256"]:
                    return False
        except OSError:
            return False
        for name, size in p.get("dlls", {}).items():
            f = os.path.join(self.dest, name)
            if not os.path.isfile(f) or os.path.getsize(f) != size:
                return False
        return True

    def _check_disk(self, todo: list[dict]):
        if not todo:
            return
        need = 0
        for p in todo:
            part = os.path.join(self.dl_dir, p["filename"] + ".part")
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            need += max(0, p["size"] - have) + sum(p.get("dlls", {}).values())
        need += 200 << 20
        try:
            free = shutil.disk_usage(self.dest).free
        except OSError:
            return
        if free < need:
            drive = os.path.splitdrive(self.dest)[0] or self.dest
            raise InstallError(f"磁盘空间不足：安装显卡运行库需要约 {_gb(need)}，{drive} 只剩 {_gb(free)}。"
                               "请清理磁盘后重试，或者改用 CPU")

    # ------------------------------------------------------------------
    def _rank_sources(self, pkgs: list[dict]) -> list[dict]:
        srcs = sources_for(self.mode)
        if self.mode != "auto" or len(srcs) <= 1:
            return srcs
        probe = max(pkgs, key=lambda p: p["size"])

        def speed(src):
            import httpx

            t0 = time.monotonic()
            got = 0
            try:
                with self._client.stream("GET", src["base"] + probe["path"],
                                         headers={"Range": f"bytes=0-{SPEED_TEST_BYTES - 1}"},
                                         timeout=httpx.Timeout(SPEED_TEST_SECONDS + 2, connect=5.0)) as r:
                    if r.status_code not in (200, 206):
                        return src, 0.0, f"HTTP {r.status_code}"
                    for chunk in r.iter_bytes(64 << 10):
                        got += len(chunk)
                        if (got >= SPEED_TEST_BYTES or time.monotonic() - t0 > SPEED_TEST_SECONDS
                                or self._cancel.is_set()):
                            break
            except Exception as e:
                if not got:
                    return src, 0.0, _describe_error(e)
            dt = max(1e-3, time.monotonic() - t0)
            return src, got / dt, ""

        with ThreadPoolExecutor(max_workers=len(srcs)) as ex:
            results = list(ex.map(speed, srcs))
        self._check_cancel()
        for src, sp, err in results:
            print(f"[runtime] 测速 {src['name']}：" + (f"{sp / 2**20:.2f} MB/s" if sp else f"失败（{err}）"),
                  file=sys.stderr)
        ok = sorted((r for r in results if r[1] > 0), key=lambda r: -r[1])
        bad = [r for r in results if r[1] <= 0]
        # 测速失败的源排在最后（也许只是测速那一下没连上），仍然可以兜底
        return [r[0] for r in ok] + [r[0] for r in bad]

    # ------------------------------------------------------------------
    def _download_verified(self, src: dict, p: dict, done_before: int, total: int) -> str:
        """从 src 下载 p 并校验 sha256。校验不对删掉重下一次，还不对就算这个源不行。"""
        part = os.path.join(self.dl_dir, p["filename"] + ".part")
        for attempt in range(2):
            self._download(src, p, part, done_before, total)
            self._check_cancel()
            if self._sha256(part) == p["sha256"]:
                return part
            print(f"[runtime] {p['filename']} 校验失败（sha256 不对），删掉重新下载", file=sys.stderr)
            os.remove(part)
        raise _SourceError("文件校验失败（下载到的内容不对）")

    def _download(self, src: dict, p: dict, part: str, done_before: int, total: int):
        import httpx

        size = p["size"]
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if have > size:
            os.remove(part)
            have = 0
        if have == size:
            return                      # 上次已经下完（等着校验）
        url = src["base"] + p["path"]
        headers = {"Range": f"bytes={have}-"} if have else {}
        print(f"[runtime] 从 {src['name']} 下载 {p['filename']}"
              + (f"（从 {have / 2**20:.0f} MB 处续传）" if have else ""), file=sys.stderr)
        try:
            with self._client.stream("GET", url, headers=headers) as r:
                with self._resp_lock:
                    self._resp = r
                self._check_cancel()
                if r.status_code == 206 and have:
                    cr = r.headers.get("content-range", "")
                    if not cr.startswith(f"bytes {have}-"):
                        raise _SourceError(f"服务器续传位置不对（{cr}）")
                    mode = "ab"
                elif r.status_code == 200:
                    have, mode = 0, "wb"     # 服务器不支持续传：从头下
                elif r.status_code == 416 and have:
                    os.remove(part)          # 本地的 .part 和服务器对不上：下次从头来
                    raise _SourceError("续传失败（416）")
                elif r.status_code == 404:
                    raise _SourceError("下载源上没有这个文件（404）")
                else:
                    raise _SourceError(f"服务器返回错误 {r.status_code}")
                try:
                    f = open(part, mode)
                except OSError as e:
                    raise InstallError(f"无法写入 {part}：{e}")
                with f:
                    for chunk in r.iter_bytes(1 << 20):
                        if self._cancel.is_set():
                            raise Cancelled()
                        try:
                            f.write(chunk)
                        except OSError as e:
                            raise InstallError(f"写入运行库文件失败（磁盘满了？）：{e}。请清理磁盘后重试，或者改用 CPU")
                        have += len(chunk)
                        if have > size:
                            raise _SourceError("下载到的文件比预期的大")
                        self._report(done_before + have, total, src["name"])
        except (Cancelled, InstallError, _SourceError):
            raise
        except Exception as e:
            if self._cancel.is_set():       # cancel() 关掉了连接，读取那边会抛各种异常
                raise Cancelled()
            if isinstance(e, (httpx.HTTPError, httpx.StreamError, OSError)):
                raise _SourceError(_describe_error(e))
            raise
        finally:
            with self._resp_lock:
                self._resp = None
        if have != size:
            raise _SourceError("连接中断，文件没下完")

    def _sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                self._check_cancel()
                b = f.read(8 << 20)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    # ------------------------------------------------------------------
    def _extract(self, p: dict, wheel: str):
        """只解压 nvidia/*/bin/*.dll，平铺到运行库目录；解压完删掉 wheel。"""
        names = []
        try:
            with zipfile.ZipFile(wheel) as z:
                for info in z.infolist():
                    parts = info.filename.split("/")
                    if not (len(parts) == 4 and parts[0] == "nvidia" and parts[2] == "bin"
                            and parts[3].lower().endswith(".dll")):
                        continue
                    self._check_cancel()
                    target = os.path.join(self.dest, parts[3])
                    tmp = target + ".tmp"
                    with z.open(info) as src, open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst, 8 << 20)
                    os.replace(tmp, target)
                    names.append(parts[3])
        except (Cancelled, InstallError):
            raise
        except zipfile.BadZipFile as e:
            os.remove(wheel)
            raise InstallError(f"显卡运行库安装包损坏（{p['filename']}：{e}），已删除，请重试。{HINT}")
        except OSError as e:
            raise InstallError(f"解压显卡运行库失败（{e}）。如果磁盘满了请清理后重试；"
                               "如果提示文件被占用，请关闭其它正在运行的听译后重试")
        missing = set(p.get("dlls", {})) - set(names)
        if missing:
            raise InstallError(f"显卡运行库安装包里缺少 {', '.join(sorted(missing))}。{HINT}")
        with open(self._done_marker(p), "w", encoding="utf-8") as f:
            f.write(p["sha256"])
        os.remove(wheel)
        print(f"[runtime] 已解压 {p['filename']}：{len(names)} 个 DLL", file=sys.stderr)

    def _finish(self, manifest: dict, mhash: str):
        expected = _expected_dlls(manifest)
        # 清掉旧版本留下的、清单里没有的 DLL
        for name in os.listdir(self.dest):
            if name.lower().endswith((".dll", ".tmp")) and name not in expected:
                try:
                    os.remove(os.path.join(self.dest, name))
                except OSError:
                    pass
        marker = {
            "manifest_sha256": mhash,
            "packages": {p["name"]: p["version"] for p in manifest["packages"]},
            "dlls": sorted(expected),
            "source": self.source_used,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = os.path.join(self.dest, MARKER + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(self.dest, MARKER))
        shutil.rmtree(self.dl_dir, ignore_errors=True)
        print(f"[runtime] 显卡运行库已就绪：{self.dest}", file=sys.stderr)
