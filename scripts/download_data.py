# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D1 数据下载脚本
=================================
下载两个公开数据集（走 HuggingFace 中国镜像 hf-mirror.com，国内直连超时）：

1. EAVSD 电商广告视频分镜数据集（zjyao-PKU/EAVSD）
   - 只下载 metadata/*.parquet（8 个 category 文件，共约 25MB）
   - 跳过 shards/*.tar（视频原文件包，每个 200MB+，本项目不需要视频本体）
2. Seedance 2 提示词数据集（GokuScraper/seedance-2-prompts-datasets）
   - 只下载 metadata.jsonl（约 38MB 纯文本）
   - 跳过 covers/videos/ref_images（媒体文件，不需要）

特性：
- 幂等：目标文件已存在则跳过，可安全重复执行
- 记录下载清单（时间/来源/大小）到 data/raw/download_manifest.json
- 总下载量约 63MB，成本 ¥0

用法：python scripts/download_data.py
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# 国内网络：HuggingFace 官方域名直连超时，hf-mirror.com 镜像可用
MIRROR = "https://hf-mirror.com"

# 仓库与需要下载的文件（相对仓库根目录）
EAVSD_REPO = "zjyao-PKU/EAVSD"
EAVSD_FILES = [f"metadata/category_{c}.parquet" for c in "abcdefgh"]

SEEDANCE_REPO = "GokuScraper/seedance-2-prompts-datasets"
SEEDANCE_FILES = ["metadata.jsonl"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
MANIFEST_PATH = DATA_DIR / "download_manifest.json"

CHUNK = 1024 * 256  # 256KB


def download(url: str, dest: Path) -> bool:
    """下载单个文件，返回是否真的下载了（False = 已存在跳过）。"""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} 已存在（{dest.stat().st_size/1e6:.1f}MB），跳过")
        return False
    print(f"  [down] {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    t0 = time.time()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done / total * 100
                print(f"\r  {dest.name}: {done/1e6:.1f}/{total/1e6:.1f}MB ({pct:.0f}%)", end="", flush=True)
    print()
    tmp.rename(dest)
    print(f"  [done] {dest.name} ({dest.stat().st_size/1e6:.1f}MB, {time.time()-t0:.0f}s)")
    return True


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": MIRROR, "files": []}

    jobs = [
        (EAVSD_REPO, EAVSD_FILES, "EAVSD 电商广告视频分镜"),
        (SEEDANCE_REPO, SEEDANCE_FILES, "Seedance-2 提示词"),
    ]

    for repo, files, label in jobs:
        print(f"\n=== {label}（{repo}）===")
        for rel in files:
            url = f"{MIRROR}/datasets/{repo}/resolve/main/{rel}"
            dest = DATA_DIR / Path(rel).name
            try:
                downloaded = download(url, dest)
                manifest["files"].append({
                    "repo": repo, "path": rel, "file": dest.name,
                    "size_bytes": dest.stat().st_size,
                    "downloaded": downloaded,
                })
            except Exception as e:  # noqa: BLE001
                print(f"  [FAIL] {rel}: {e!r}")
                manifest["files"].append({"repo": repo, "path": rel, "error": repr(e)})

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n清单已写入 {MANIFEST_PATH}")
    print("下载完成。接下来运行清洗脚本：python scripts/clean_data.py")


if __name__ == "__main__":
    sys.exit(main())
