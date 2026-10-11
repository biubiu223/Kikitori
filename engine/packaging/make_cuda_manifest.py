"""生成 engine/packaging/cuda_manifest.json：显卡运行库（NVIDIA 在 PyPI 上发布的官方 wheel）的固定版本清单。

用法（在仓库根目录，用打包引擎的同一个 Python 环境）：
    .venv\\Scripts\\python.exe engine\\packaging\\make_cuda_manifest.py

做的事：
  1. 查出当前环境里已安装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cuda-nvrtc-cu12 的版本
  2. 到 PyPI JSON API 查这个版本的 win_amd64 wheel：文件名、packages/... 路径、大小、sha256
  3. 从本地安装记录（RECORD）里读出 wheel 里 nvidia/*/bin/*.dll 的文件名和大小（引擎用来检查是否装完整、估算磁盘空间）
结果写进 engine/packaging/cuda_manifest.json（提交进仓库；打包时一起带上，见 engine.spec）。
引擎按清单下载，清单内容变了（升级 CUDA 版本）用户那边会自动重新下载。
"""

import importlib.metadata as md
import json
import sys
from pathlib import Path

import httpx

PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12", "nvidia-cuda-nvrtc-cu12"]
OUT = Path(__file__).resolve().parent / "cuda_manifest.json"


def local_dlls(dist_name: str) -> dict[str, int]:
    """已安装的这个包里 nvidia/<子包>/bin/*.dll → 大小。"""
    out = {}
    for f in md.files(dist_name) or []:
        parts = f.parts
        if len(parts) == 4 and parts[0] == "nvidia" and parts[2] == "bin" and parts[3].lower().endswith(".dll"):
            size = f.size
            if size is None:
                size = Path(f.locate()).stat().st_size
            out[parts[3]] = int(size)
    return dict(sorted(out.items()))


def wheel_info(client: httpx.Client, name: str, version: str) -> dict:
    r = client.get(f"https://pypi.org/pypi/{name}/{version}/json")
    r.raise_for_status()
    wheels = [u for u in r.json()["urls"]
              if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-win_amd64.whl")]
    if not wheels:
        raise SystemExit(f"PyPI 上找不到 {name} {version} 的 win_amd64 wheel")
    u = wheels[0]
    prefix = "https://files.pythonhosted.org/"
    if not u["url"].startswith(prefix + "packages/"):
        raise SystemExit(f"意外的下载地址：{u['url']}")
    return {"filename": u["filename"], "path": u["url"][len(prefix):], "size": int(u["size"]),
            "sha256": u["digests"]["sha256"]}


def main():
    packages = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for name in PACKAGES:
            try:
                version = md.version(name)
            except md.PackageNotFoundError:
                raise SystemExit(f"当前环境没有安装 {name}（先 pip install -r engine\\requirements-cuda.txt）")
            info = wheel_info(client, name, version)
            dlls = local_dlls(name)
            if not dlls:
                raise SystemExit(f"{name} 的安装记录里没有 nvidia/*/bin/*.dll")
            packages.append({"name": name, "version": version, **info, "dlls": dlls})
            print(f"{name} {version}: {info['filename']}  {info['size'] / 2**20:.0f} MB，"
                  f"{len(dlls)} 个 DLL 共 {sum(dlls.values()) / 2**20:.0f} MB")

    manifest = {
        "format": 1,
        "description": "Kikitori 显卡运行库清单（NVIDIA 官方 wheel，固定版本）。由 engine/packaging/make_cuda_manifest.py 生成，不要手改。",
        "packages": packages,
    }
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total = sum(p["size"] for p in packages)
    print(f"已写入 {OUT}（下载共 {total / 2**20:.0f} MB）")


if __name__ == "__main__":
    sys.exit(main())
