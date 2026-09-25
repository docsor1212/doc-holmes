"""C 级扫描件 OCR 适配层（实验性，预览质量）。

通道：Tesseract 5（CPU；能力探测，缺失时报清晰指引而不是崩溃）。
做法：fitz 逐页栅格化 → tesseract 生成「图像+隐形文本层」单页 PDF → 合并。
得到的可译 PDF 再交给引擎走正常翻译通路。
单页超时不中断：丢文本层保页面完整，保证 C 级完成率优先（宪章铁律）。
产物强制附「预览质量」说明页（attach_preview_notice）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pymupdf

DEFAULT_DPI = 200
DEFAULT_PSM = "6"
DEFAULT_LANG = "eng"
PAGE_TIMEOUT_S = 120

NOTICE_ZH = (
    "预览质量说明\n\n"
    "原文件为扫描件/无文本层，本文由 doc-holmes OCR 实验通道生成：\n"
    "先光学识别再翻译，可能存在识别错误，仅作预览参考，\n"
    "不应用于临床决策、投稿或正式引用。")
NOTICE_EN = (
    "Preview quality notice\n\n"
    "The source was a scanned / image-only PDF. This document was produced by\n"
    "the doc-holmes experimental OCR pipeline (recognize, then translate).\n"
    "Recognition errors are possible. For preview only - NOT for clinical use,\n"
    "submission, or formal citation.")


def tesseract_info() -> dict:
    """能力探测：tesseract 可执行文件、版本、可用语言包。"""
    info = {"available": False}
    if os.environ.get("DOC_HOLMES_OCR_OFF") == "1":
        info["reason"] = "DOC_HOLMES_OCR_OFF=1（已显式关闭 OCR）"
        return info
    binary = shutil.which("tesseract") or os.environ.get("DOC_HOLMES_TESSERACT_BIN")
    if not binary or not os.path.isfile(binary):
        info["reason"] = "未安装 tesseract。安装：apt install tesseract-ocr tesseract-ocr-chi-sim"
        return info
    info["binary"] = binary
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                             timeout=15)
        info["version"] = (out.stdout or out.stderr).splitlines()[0].strip()
    except Exception as exc:
        info["reason"] = "tesseract 无法执行：%s" % exc
        return info
    try:
        langs = subprocess.run([binary, "--list-langs"], capture_output=True,
                               text=True, timeout=15).stdout
        info["langs"] = [x.strip() for x in langs.splitlines()[1:] if x.strip()]
    except Exception:
        info["langs"] = []
    info["available"] = True
    return info


def ocr_pdf_to_textlayer(pdf: str, out_pdf: str, *, dpi: int = DEFAULT_DPI,
                         lang: str = DEFAULT_LANG, psm: str = DEFAULT_PSM,
                         page_timeout: int = PAGE_TIMEOUT_S,
                         progress=None) -> dict:
    """扫描 PDF → 图像+隐形文本层 PDF。返回统计（页数/有文本层页数/OCR 字符数）。"""
    info = tesseract_info()
    if not info.get("available"):
        raise RuntimeError(info.get("reason", "tesseract 不可用"))
    missing = [x for x in lang.split("+") if x not in info.get("langs", [])]
    if missing:
        raise RuntimeError(
            "tesseract 缺少语言包 %s（已有：%s）。安装示例：\n"
            "  apt install tesseract-ocr-<lang>\n"
            "或改用 --ocr-lang 选择已安装语言。" % (missing, ",".join(info.get("langs", [])) or "无"))

    stats = {"pages": 0, "pages_with_text": 0, "ocr_chars": 0,
             "pages_timed_out": 0, "pages_ocr_failed": 0}
    os.makedirs(os.path.dirname(os.path.realpath(out_pdf)) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doc_holmes_ocr_") as tmp:
        out_doc = pymupdf.open()
        src = pymupdf.open(pdf)
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        for i in range(src.page_count):
            stats["pages"] += 1
            page = src[i]
            pix = page.get_pixmap(matrix=mat)
            img_path = os.path.join(tmp, "p%04d.png" % i)
            pix.save(img_path)
            base = os.path.join(tmp, "p%04d" % i)
            page_pdf = base + ".pdf"
            try:
                # 单次调用同时产出 pdf + txt（tesseract 支持多输出配置，省一半 OCR 时间）
                subprocess.run(
                    [info["binary"], img_path, base, "-l", lang, "--psm", psm,
                     "pdf", "txt"],
                    capture_output=True, timeout=page_timeout, check=True)
                txt_path = base + ".txt"
                txt = ""
                if os.path.isfile(txt_path):
                    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
                        txt = f.read()
                single = pymupdf.open(page_pdf)
                out_doc.insert_pdf(single)
                single.close()
                chars = len("".join(txt.split()))
                stats["ocr_chars"] += chars
                if chars > 0:
                    stats["pages_with_text"] += 1
            except subprocess.TimeoutExpired:
                # 超时页：保页面（仅图像），不丢页
                img_doc = pymupdf.open()
                img_page = img_doc.new_page(width=page.rect.width, height=page.rect.height)
                img_page.insert_image(page.rect, pixmap=pix)
                out_doc.insert_pdf(img_doc)
                img_doc.close()
                stats["pages_timed_out"] += 1
            except Exception:
                img_doc = pymupdf.open()
                img_page = img_doc.new_page(width=page.rect.width, height=page.rect.height)
                img_page.insert_image(page.rect, pixmap=pix)
                out_doc.insert_pdf(img_doc)
                img_doc.close()
                stats["pages_ocr_failed"] += 1
            if progress:
                progress(i + 1, src.page_count)
        src.close()
        out_doc.save(out_pdf, garbage=3, deflate=True)
        out_doc.close()
    return stats


def attach_preview_notice(pdf_path: str, out_path: str = None) -> str:
    """在文档开头插入「预览质量」说明页（中英双语）。返回新文件路径。"""
    target = out_path or pdf_path
    doc = pymupdf.open(pdf_path)
    if doc.page_count == 0:
        doc.close()
        return target
    notice = pymupdf.open()
    page = notice.new_page(width=doc[0].rect.width, height=doc[0].rect.height)
    rect = page.rect + (72, 72, -72, -72)
    page.insert_textbox(rect, NOTICE_ZH, fontsize=14, fontname="china-s",
                        color=(0.85, 0.15, 0.15), align=0)
    page.insert_textbox(rect + (0, rect.height * 0.45, 0, 0), NOTICE_EN,
                        fontsize=11, fontname="helv", color=(0.2, 0.2, 0.2), align=0)
    doc.insert_pdf(notice, start_at=0)
    notice.close()
    if out_path and os.path.realpath(out_path) != os.path.realpath(pdf_path):
        try:
            doc.save(out_path, garbage=3, deflate=True)
        finally:
            doc.close()
        return target
    tmp = pdf_path + ".notice.tmp.pdf"
    try:
        doc.save(tmp, garbage=3, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, pdf_path)
    return pdf_path


# ==================== v2.0.0：TSV 行框重建修复文本层 ====================

def _repair_line(line: str) -> str:
    """行级修复：控制字符剥除 + 中文字间伪空格。粘连拆分在行级不适用（会改变框内词距）。"""
    from . import skip_rules
    return skip_rules.strip_control_chars(line)


def rebuild_repaired_textlayer(pdf: str, out_pdf: str, *, lang: str = "eng",
                               dpi: int = DEFAULT_DPI, psm: str = DEFAULT_PSM,
                               page_timeout: int = PAGE_TIMEOUT_S,
                               progress=None) -> dict:
    """C 级通路 v2：自建"图像 + 修复后隐形文本层"PDF。

    流程：逐页栅格化 → tesseract TSV（词级框）→ 行聚合 → 行级文本修复
    （控制字符剥除 + 中文伪空格删除，V5 修复层首次真实作用于译文源）
    → pymupdf 以 render_mode=3（不可见）写入行框文本。
    tesseract 不可用/语言包缺失时抛 RuntimeError（与旧通道同口径）。
    """
    info = tesseract_info()
    if not info.get("available"):
        raise RuntimeError(info.get("reason", "tesseract 不可用"))
    missing = [x for x in lang.split("+") if x not in info.get("langs", [])]
    if missing:
        raise RuntimeError(
            "tesseract 缺少语言包 %s（已有：%s）。安装示例：apt install tesseract-ocr-<lang>"
            % (missing, ",".join(info.get("langs", [])) or "无"))

    from .repair_v5 import fix_cjk_spaces
    from . import skip_rules

    stats = {"pages": 0, "lines": 0, "lines_repaired": 0, "chars": 0,
             "pages_ocr_failed": 0}
    os.makedirs(os.path.dirname(os.path.realpath(out_pdf)) or ".", exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="doc_holmes_ocr_") as tmp:
        out_doc = pymupdf.open()
        src = pymupdf.open(pdf)
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        for i in range(src.page_count):
            page = src[i]
            stats["pages"] += 1
            img_path = os.path.join(tmp, "p%04d.png" % i)
            pix = page.get_pixmap(matrix=mat)
            pix.save(img_path)
            base = os.path.join(tmp, "p%04d" % i)
            try:
                subprocess.run(
                    [info["binary"], img_path, base, "-l", lang, "--psm", psm, "tsv"],
                    capture_output=True, timeout=page_timeout, check=True)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                stats["pages_ocr_failed"] += 1
                fail_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
                fail_page.insert_image(fail_page.rect, pixmap=pix)  # 不丢页：保留扫描图像
                if progress:
                    progress(i + 1, src.page_count)
                continue
            # 解析 TSV，按 (block, par, line) 聚合词框
            words = []
            tsv_path = base + ".tsv"
            if os.path.isfile(tsv_path):
                with open(tsv_path, encoding="utf-8", errors="ignore") as f:
                    header = f.readline()
                    if "\t" not in header:
                        continue  # 空/坏 TSV：整页按无文本处理
                    for row in f:
                        cols = row.rstrip("\r\n").split("\t")
                        if len(cols) < 12 or cols[11].strip() == "":
                            continue
                        try:
                            # TSV 列序：level page block par line word left top w h conf text
                            lvl, bk, par, ln = (int(cols[0]), int(cols[2]),
                                                int(cols[3]), int(cols[4]))
                        except (ValueError, IndexError):
                            continue
                        if lvl != 5 or len(cols) < 12:
                            continue
                        left, top, w, h = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9])
                        words.append((bk, par, ln, left, top, w, h, cols[11]))
            # 聚合成行
            lines = {}
            for bk, par, ln, left, top, w, h, text in words:
                lines.setdefault((bk, par, ln), []).append((left, top, w, h, text))
            zoom_inv = 72.0 / dpi
            out_page = out_doc.new_page(width=page.rect.width, height=page.rect.height)
            out_page.insert_image(out_page.rect, pixmap=pix)
            for (_bk, _par, _ln), ws in lines.items():
                ws.sort(key=lambda x: x[0])
                raw = " ".join(t for *_, t in ws)
                fixed = _repair_line(raw)
                fixed = fix_cjk_spaces(fixed)[0]
                if not fixed.strip():
                    continue
                x0 = min(w[0] for w in ws) * zoom_inv
                y1 = max(w[1] + w[3] for w in ws) * zoom_inv
                hh = max(w[3] for w in ws) * zoom_inv
                fontsize = max(6.0, min(hh * 0.85, 42.0))
                # 非 Latin-1 字符（CJK/全角标点等）必须走 CJK 内置字体：
                # helv 遇到它们不报错而是静默写 notdef 豆腐（实测），比抛异常更危险
                if all(ord(ch) < 256 for ch in fixed):
                    fname = "helv"
                else:
                    fname = "china-s"
                try:
                    out_page.insert_text((x0, y1 - hh * 0.15), fixed,
                                         fontsize=fontsize, fontname=fname,
                                         render_mode=3)  # 3 = 不可见
                except Exception:
                    continue
                stats["lines"] += 1
                if fixed != " ".join(t for *_, t in ws):
                    stats["lines_repaired"] += 1
                stats["chars"] += len(fixed)
            if progress:
                progress(i + 1, src.page_count)
        src.close()
        out_doc.save(out_pdf, garbage=3, deflate=True)
        out_doc.close()
    return stats
