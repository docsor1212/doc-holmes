#!/usr/bin/env python3
"""DOCX/PPTX/PPT 输入转换（plan T6.3）：libreoffice headless → PDF → 现有管线。

设计（AF 教训：环境限制要"识别+指引"而非"重试"）：
- 仅当用户显式给了 .docx/.pptx/.ppt 输入时进入本模块；
- libreoffice 缺失 → 可操作报错（含安装命令），绝不硬试；
- 转换产物落在用户输出目录，转换失败给出可操作信息。
"""

from __future__ import annotations

import os
import shutil
import subprocess

SUPPORTED = {".docx": "docx", ".pptx": "impress", ".ppt": "impress"}


_CANDIDATES = (
    "/snap/bin/libreoffice", "/usr/bin/libreoffice", "/usr/local/bin/libreoffice",
    "/snap/bin/soffice", "/usr/bin/soffice",
    "/opt/libreoffice*/program/soffice",
)


def libreoffice_bin() -> str | None:
    """PATH 探测 + 常见绝对路径候选（snap 版常不在非交互 PATH）。"""
    for name in ("libreoffice", "soffice"):
        found = shutil.which(name)
        if found:
            return found
    import glob
    for cand in _CANDIDATES:
        hits = glob.glob(cand)
        if hits and os.path.isfile(hits[0]) and os.access(hits[0], os.X_OK):
            return hits[0]
    return None


def conversion_guidance(input_path: str) -> str:
    return (
        "[转换] 本机未安装 libreoffice，无法把 %s 转成 PDF。\n"
        "  · Debian/Ubuntu：apt install libreoffice\n"
        "  · macOS：brew install --cask libreoffice\n"
        "  · 或先手动另存为 PDF 后重试。" % os.path.basename(input_path))


def convert_to_pdf(input_path: str, outdir: str, timeout: int = 300) -> str:
    """libreoffice headless 转 PDF，返回产物路径。失败抛 RuntimeError（含 stderr 尾部）。"""
    binpath = libreoffice_bin()
    if not binpath:
        raise RuntimeError(conversion_guidance(input_path))
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in SUPPORTED:
        raise RuntimeError("不支持的输入格式：%s（支持 .docx/.pptx/.ppt）" % ext)
    # snap 版 libreoffice 的沙箱读不到 /tmp：源与产物统一走 HOME 暂存目录
    staging = os.path.join(os.path.expanduser("~"), "doc-holmes-office")  # snap home 接口不覆盖点目录
    os.makedirs(staging, exist_ok=True)
    os.makedirs(outdir, exist_ok=True)
    staged_src = os.path.join(staging, os.path.basename(input_path))
    shutil.copyfile(input_path, staged_src)
    try:
        proc = subprocess.run(
            [binpath, "--headless", "--convert-to", "pdf",
             "--outdir", staging, staged_src],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("libreoffice 转换超时（%ds）：%s" % (timeout, os.path.basename(input_path)))
    produced = os.path.join(
        staging, os.path.splitext(os.path.basename(input_path))[0] + ".pdf")
    if proc.returncode != 0 or not os.path.isfile(produced):
        raise RuntimeError("libreoffice 转换失败（rc=%s）：%s"
                           % (proc.returncode, (proc.stderr or "")[-200:]))
    final = os.path.join(outdir, os.path.basename(produced))
    shutil.move(produced, final)
    return final
