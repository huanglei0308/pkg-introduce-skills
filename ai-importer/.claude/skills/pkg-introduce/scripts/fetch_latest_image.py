#!/usr/bin/env python3
"""
获取 openEuler Docker 镜像

功能：
  1. 从 build-env.conf.json 读取版本/架构配置
  2. 下载 openEuler-docker.<arch>.tar.xz
  3. 加载到本地 Docker（可选）

用法：
  python fetch_latest_image.py [--download] [--load] [--output-dir /path]
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests

_SCRIPT_DIR = Path(__file__).resolve().parent
_CONF_PATH = _SCRIPT_DIR.parent / "build-env.conf.json"

def _load_conf() -> dict:
    if not _CONF_PATH.exists():
        print(f"[ERROR] 配置文件不存在: {_CONF_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(_CONF_PATH) as f:
        return json.load(f)

_CONF = _load_conf()
_IMG = _CONF["image"]

BASE_URL = _IMG["base_url"]
BRANCH = _IMG["branch"]
BUILD = _IMG["build"]
ARCH = _IMG["arch"]
RELEASE_ROOT = f"{BASE_URL}/{BRANCH}/{BUILD}"
IMAGE_FILENAME = f"{_IMG['filename_prefix']}.{ARCH}.tar.xz"
IMAGE_URL = f"{RELEASE_ROOT}/docker_img/{ARCH}/{IMAGE_FILENAME}"


def get_expected_hash(sha256sum_url: str) -> Optional[str]:
    """获取远端 sha256sum 中的期望哈希值"""
    try:
        resp = requests.get(sha256sum_url, timeout=10)
        resp.raise_for_status()
        # 格式：<hash>  <filename>
        return resp.text.strip().split()[0]
    except Exception as e:
        print(f"[ERROR] 无法获取校验文件: {e}", file=sys.stderr)
        return None


def calculate_sha256(file_path: Path) -> str:
    """计算本地文件 SHA256"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def verify_checksum(file_path: Path, expected_hash: str) -> bool:
    """校验本地文件 SHA256"""
    print(f"[INFO] 校验 SHA256: {expected_hash[:16]}...")
    actual = calculate_sha256(file_path)

    if actual == expected_hash:
        print("[INFO] 校验通过")
        return True

    print(f"[ERROR] 校验失败: 期望 {expected_hash}, 实际 {actual}", file=sys.stderr)
    return False


def download_image(url: str, output_dir: Path, expected_hash: str) -> Path:
    """下载最新镜像；仅当本地缓存与远端 latest 一致时复用"""
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / IMAGE_FILENAME

    if dest.exists():
        print(f"[INFO] 检查本地缓存: {dest}")
        local_hash = calculate_sha256(dest)
        print(f"[INFO] 本地缓存 SHA256: {local_hash[:16]}...")
        if local_hash == expected_hash:
            print("[INFO] 本地缓存与远端 latest 一致，复用缓存")
            return dest
        print("[INFO] 本地缓存与远端 latest 不一致，重新下载最新镜像")

    print(f"[INFO] 开始下载: {url}")
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                print(f"\r[INFO] 下载进度: {pct:.1f}% ({downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB)", end="")

    print()
    print(f"[INFO] 下载完成: {dest}")
    return dest


def load_docker_image(tar_path: Path) -> str:
    """将 tar.xz 加载到 Docker，返回镜像名"""
    print(f"[INFO] 加载 Docker 镜像: {tar_path}")
    try:
        result = subprocess.run(
            ["docker", "load", "-i", str(tar_path)],
            capture_output=True, text=True, check=True
        )
        # 输出格式：Loaded image: openeuler:latest 或 Loaded image ID: sha256:...
        output = result.stdout.strip()
        print(f"[INFO] {output}")
        m = re.search(r"Loaded image:\s*(\S+)", output)
        return m.group(1) if m else output
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] docker load 失败: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("[ERROR] Docker 未安装或不在 PATH 中", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="获取 openEuler Docker 镜像（配置见 build-env.conf.json）")
    parser.add_argument("--download", action="store_true", help="下载镜像文件")
    parser.add_argument("--load", action="store_true", help="加载镜像到 Docker（自动启用 --download）")
    parser.add_argument("--output-dir", default="./images", help="镜像保存目录（默认 ./images）")
    parser.add_argument("--list", action="store_true", help="仅输出固定镜像 URL，不下载")
    args = parser.parse_args()

    if args.load:
        args.download = True

    image_url = IMAGE_URL
    sha256_url = image_url + ".sha256sum"

    print(f"[INFO] 配置文件: {_CONF_PATH}")
    print(f"[INFO] 发布根目录: {RELEASE_ROOT}")
    print(f"[INFO] 镜像 URL: {image_url}")

    if args.list:
        print(f"\nIMAGE_URL={image_url}")
        return

    expected_hash = get_expected_hash(sha256_url)
    if not expected_hash:
        sys.exit(1)

    if not args.download:
        print(f"\nIMAGE_URL={image_url}")
        return

    output_dir = Path(args.output_dir)
    tar_path = download_image(image_url, output_dir, expected_hash)
    if not verify_checksum(tar_path, expected_hash):
        sys.exit(1)

    if args.load:
        image_name = load_docker_image(tar_path)
        print(f"\nDOCKER_IMAGE={image_name}")
    else:
        print(f"\nTAR_PATH={tar_path}")


if __name__ == "__main__":
    main()
