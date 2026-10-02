#!/usr/bin/env python3
"""加密 PDF 解密 + 术语表智能种子（v2.7.0）。

qpdf --decrypt 解密密码保护 PDF 后走现有管线。
高频领域术语自动抽取：从源 PDF 提取高频英文术语（≥3 字母，非停用词），
与内置医学术语表合并注入 BabelDOC --glossaries 参数。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections import Counter


def needs_decrypt(pdf_path: str) -> bool:
    import pymupdf
    try:
        doc = pymupdf.open(pdf_path)
        encrypted = doc.needs_pass
        doc.close()
        return encrypted
    except Exception:
        return False


def decrypt_pdf(pdf_path: str, password: str, outdir: str) -> str:
    """qpdf --decrypt 解密 PDF。返回解密后路径。"""
    qpdf = shutil_which("qpdf")
    if not qpdf:
        raise RuntimeError(
            "qpdf 未安装。解密密码保护 PDF 需要 qpdf：\n"
            "  · Debian/Ubuntu：apt install qpdf\n"
            "  · 或提供已解密的 PDF 文件。")
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    out_path = os.path.join(outdir, stem + "_decrypted.pdf")
    cmd = [qpdf, "--password", password, "--decrypt", pdf_path, out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("qpdf 解密失败（rc=%d）：%s"
                           % (proc.returncode, (proc.stderr or "")[-200:]))
    return out_path


def shutil_which(name):
    from shutil import which
    return which(name)


# ===== 术语表智能种子 =====

# 通用英文停用词（简化版，仅覆盖高频功能词）
_STOP = {"the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
         "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
         "how", "its", "new", "now", "old", "see", "two", "way", "who", "did",
         "each", "that", "this", "with", "from", "they", "have", "been", "were",
         "their", "which", "will", "would", "about", "there", "these", "other",
         "could", "than", "then", "them", "more", "when", "what", "into", "time",
         "only", "over", "such", "most", "also", "may", "should", "after",
         "between", "through", "our", "these", "some", "than", "then", "well"}


def extract_domain_terms(text: str, min_len: int = 5, top_n: int = 20) -> list:
    """从源 PDF 文本提取高频领域术语（≥min_len 字母、非停用词、按频次排序）。"""
    words = re.findall(r"[a-zA-Z]{%d,}" % min_len, text)
    stop = {w.lower() for w in _STOP}
    terms = Counter(
        w for w in words
        if w.lower() not in stop and not w.isupper() and len(w) >= min_len)
    return [term for term, _ in terms.most_common(top_n)]


def seed_glossary(pdf_path: str, built_in_path: str, out_dir: str,
                  top_n: int = 20, min_len: int = 5) -> str:
    """从源 PDF 抽取高频术语 + 内置医学表 → 合并 CSV（BabelDOC --glossaries 格式）。"""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    text = " ".join(doc[i].get_text() for i in range(min(doc.page_count, 5)))
    doc.close()
    domain = extract_domain_terms(text, min_len=min_len, top_n=top_n)
    # 读内置表
    merged = ["source,target,tgt_lng"]
    seen = set()
    if os.path.isfile(built_in_path):
        with open(built_in_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("source,"):
                    merged.append(line)
                    seen.add(line.split(",")[0].lower())
    # 追加领域术语（不带翻译——LLM 自行翻译并保持一致）
    for term in domain:
        if term.lower() not in seen:
            merged.append("%s,,zh" % term)
            seen.add(term.lower())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "seeded_glossary.csv")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(merged) + "\n")
    return out_path
