#!/usr/bin/env python3
"""doc-holmes 命令行入口。

子命令：triage（体检分级）/ translate（单文件翻译）/ batch（批量）/
report（汇总报告）/ selfcheck（环境自检）。全部中文帮助。
退出码：0 成功；2 配置/参数错误；3 引擎失败；4 批量部分失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .endpoints import (ENV_FILE, EndpointError, PaidEndpointError,
                        load_endpoint_config)


def _print_triage(reports, as_json=False):
    if as_json:
        print(json.dumps([r.to_dict() for r in reports], ensure_ascii=False, indent=2))
        return
    header = "%-4s %-9s %8s %6s %6s  %s" % ("级别", "文件", "字符/页", "重复", "水印", "说明")
    print(header)
    print("-" * len(header.expandtabs()))
    for r in reports:
        name = os.path.basename(r.path)[:36]
        reasons = "；".join(r.reasons)[:80]
        print("%-4s %-9s %8.0f %6d %6d  %s"
              % (r.tier, name, r.chars_per_page, r.duplicate_pairs,
                 r.watermark_lines, reasons))
    tiers = [r.tier for r in reports]
    print("\n汇总: A=%d B=%d C=%d，共 %d 份"
          % (tiers.count("A"), tiers.count("B"), tiers.count("C"), len(tiers)))


def cmd_triage(args) -> int:
    from .triage import triage_path
    reports = []
    for p in args.paths:
        reports.extend(triage_path(p, sample_pages=args.sample_pages))
    if not reports:
        print("未找到 PDF 文件。", file=sys.stderr)
        return 2
    _print_triage(reports, as_json=args.json)
    return 0


AUTO_SPLIT_PAGES_THRESHOLD = 40   # 页数阈值：超过即自动分段 + 自动跳过术语抽取
AUTO_SPLIT_PART_SIZE = 25


def _large_doc_policy(page_count, part_pages, glossary):
    """大文档自动策略（v2.0.0）：返回 (effective_part_pages, effective_no_glossary, notices)。

    - part_pages: None=自动（页数≥阈值按 25 页/段）；0=显式关闭分段；N=显式段大小
    - glossary: "auto" 时大文档自动跳过术语抽取（STTT 案例：巨型提示超时）；"off" 强制关
    """
    notices = []
    eff_part = part_pages
    eff_no_glossary = (glossary == "off")
    if page_count and page_count >= AUTO_SPLIT_PAGES_THRESHOLD:
        if eff_part is None:
            eff_part = AUTO_SPLIT_PART_SIZE
            notices.append("[大文档] %d 页 ≥ %d：自动按 %d 页/段分段翻译（--part-pages 0 可关闭）"
                           % (page_count, AUTO_SPLIT_PAGES_THRESHOLD, AUTO_SPLIT_PART_SIZE))
        if glossary == "auto":
            eff_no_glossary = True
            notices.append("[大文档] 自动跳过术语抽取（防巨型提示超时；--glossary auto 可感知）")
    return eff_part, eff_no_glossary, notices


def _translate_one(pdf, outdir, ep, *, lang_in, lang_out, pages, no_dual, no_mono,
                   qps, tier_mode, ocr_mode, ocr_lang, timeout_s, repair_mode="auto",
                   no_glossary=False, part_pages=None, glossary="auto"):
    """单文件完整通路：triage → (C 级 OCR) → 引擎 → (C 级说明页) → audit。"""
    from . import triage as triage_mod
    from .engine_babeldoc import translate_pdf

    os.makedirs(outdir, exist_ok=True)
    rep = triage_mod.triage_pdf(pdf)
    tier = rep.tier if tier_mode in (None, "auto") else tier_mode
    # 大文档自动策略（v2.0.0）：分段 + 术语抽取跳过（必须在 extra 使用前计算）
    # 用户显式给 --pages 时跳过自动策略（局部小任务不该被强跳术语/强分段）；
    # 显式 --part-pages 仍被尊重
    policy_page_count = None if pages else rep.page_count
    auto_part = part_pages
    eff_part, eff_no_glossary, policy_notices = _large_doc_policy(
        policy_page_count, auto_part, glossary)
    for notice in policy_notices:
        print(notice)

    extra = []
    if rep.two_column:
        extra += ["--split-short-lines"]
    if tier == "C":
        # OCR 重建层（图像+隐形文本）会被引擎的扫插件检测拦下，此为官方通道开关
        extra += ["--skip-scanned-detection"]
    if no_glossary or eff_no_glossary:
        # 大文档术语抽取会构造巨型提示导致超时（STTT 案例），可整体关闭（append 勿覆盖）
        extra.append("--no-auto-extract-glossary")

    # B 级图层重复检测（只读；on=全文档强制含 A 级，auto=仅 B 级，off=关闭）
    repair_stats = None
    work_pdf = pdf
    if repair_mode != "off" and (repair_mode == "on" or tier == "B"):
        try:
            from .repair_pdf import inspect_pdf_dedup
            repair_stats = inspect_pdf_dedup(pdf)
        except Exception as exc:   # 检测失败不拖死翻译（评审 P1）
            repair_stats = {"error": str(exc)[:200]}
        if repair_stats.get("spans_duplicated"):
            print("[B级] 同位置重复 span：%d 个（双重打印或伪粗体；示例：%s）"
                  % (repair_stats["spans_duplicated"],
                     (repair_stats["examples"][0] if repair_stats.get("examples") else "")[:40]))

    ocr_stats = None
    tmp_ocr = None
    if tier == "C":
        info = None
        if ocr_mode != "off":
            from . import ocr_adapter
            info = ocr_adapter.tesseract_info()
        if not info or not info.get("available"):
            print("[C级] 这是扫描件/无文本层 PDF，需要 OCR 实验通道：\n"
                  "  · 安装 tesseract：apt install tesseract-ocr（中文再加 tesseract-ocr-chi-sim）\n"
                  "  · 或改用有文本层的原生 PDF（doc-holmes 对 A/B 级承诺翻译质量）\n"
                  "  · 本文件未翻译。", file=sys.stderr)
            return 2, rep, None
        print("[C级] 走 OCR 实验通道（预览质量）：%s" % info.get("version", "tesseract"))
        tmp_ocr = os.path.join(outdir, os.path.basename(pdf))
        if repair_mode == "off":
            ocr_stats = ocr_adapter.ocr_pdf_to_textlayer(pdf, tmp_ocr, lang=ocr_lang)
        else:
            # v2.0.0：自建修复后隐形文本层（行级修复真实作用于译文源）
            ocr_stats = ocr_adapter.rebuild_repaired_textlayer(pdf, tmp_ocr, lang=ocr_lang)
            print("[C级] 修复层：%d 行中修复 %d 行（%d 字符）"
                  % (ocr_stats.get("lines", 0), ocr_stats.get("lines_repaired", 0),
                     ocr_stats.get("chars", 0)))
        if "pages_with_text" in ocr_stats:      # 旧 tesseract 直出层
            print("[C级] OCR 完成：%(pages)d 页，%(pages_with_text)d 页有文本，共 %(ocr_chars)d 字符"
                  % ocr_stats)
        else:                                    # v2.0.0 修复重建层
            print("[C级] OCR 完成：%(pages)d 页，%(lines)d 行（修复 %(lines_repaired)d），共 %(chars)d 字符"
                  % ocr_stats)
        work_pdf = tmp_ocr

    if eff_part:
        extra = extra + ["--max-pages-per-part", str(eff_part)]
    eng = translate_pdf(work_pdf, outdir, ep.base_url, ep.api_key, ep.model,
                        page_count=rep.page_count, timeout_s=timeout_s,
                        lang_in=lang_in, lang_out=lang_out, qps=qps,
                        pages=pages, no_dual=no_dual, no_mono=no_mono,
                        openai_timeout=ep.timeout,
                        extra_args=extra)

    if tmp_ocr and os.path.isfile(tmp_ocr):
        try:
            os.remove(tmp_ocr)
        except OSError:
            pass

    audit = {
        "path": os.path.realpath(pdf), "tier": tier,
        "triage": rep.to_dict(), "engine": eng.to_dict(),
        "ocr": ocr_stats, "repair": repair_stats, "ok": eng.ok,
        "large_doc_policy": {"part_pages": eff_part or 0,
                             "glossary_skipped": bool(eff_no_glossary or no_glossary)},
    }
    if eng.ok:
        from .ocr_adapter import attach_preview_notice
        if tier == "C":
            for p in (eng.dual_path, eng.mono_path):
                if p:
                    attach_preview_notice(p)
            audit["preview_quality_notice"] = True
        audit_path = os.path.join(
            outdir, os.path.splitext(os.path.basename(pdf))[0] + ".audit.json")
        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)
        print("✔ 翻译完成（%s 级，%.0fs）" % (tier, eng.duration_s))
        if eng.dual_path:
            print("  双语对照: %s" % eng.dual_path)
        if eng.mono_path:
            print("  纯译文:   %s" % eng.mono_path)
        return 0, rep, audit
    print("✘ 翻译失败（%s）" % (audit["engine"]["stderr_tail"].splitlines()[-1][:160]
                             if audit["engine"]["stderr_tail"] else "原因未知"),
          file=sys.stderr)
    return 3, rep, audit


def cmd_translate(args) -> int:
    try:
        ep = load_endpoint_config()
    except (EndpointError, PaidEndpointError) as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return 2
    try:
        from .engine_babeldoc import find_engine_bin
        find_engine_bin()
    except Exception as exc:
        print("引擎不可用：%s" % exc, file=sys.stderr)
        return 2
    if os.path.isdir(args.pdf):
        print("%s 是目录——单文件请用 translate，目录批量请用：\n"
              "  doc-holmes batch %s -o <输出目录>" % (args.pdf, args.pdf), file=sys.stderr)
        return 2
    if args.part_pages is not None and args.part_pages < 0:
        print("--part-pages 不能为负数", file=sys.stderr)
        return 2
    if not os.path.isfile(args.pdf):
        print("文件不存在：%s" % args.pdf, file=sys.stderr)
        return 2
    outdir = args.output or os.path.join(
        os.path.dirname(os.path.realpath(args.pdf)) or ".", "_translated")
    code, _, _ = _translate_one(
        args.pdf, outdir, ep, lang_in=args.lang_in, lang_out=args.lang_out,
        pages=args.pages, no_dual=args.no_dual, no_mono=args.no_mono,
        qps=args.qps if args.qps is not None else ep.qps, tier_mode=args.tier, ocr_mode=args.ocr,
        ocr_lang=args.ocr_lang, timeout_s=args.timeout_s,
        repair_mode=args.repair, no_glossary=args.no_glossary,
        part_pages=args.part_pages, glossary=args.glossary)
    return code


def cmd_batch(args) -> int:
    from .runner import run_batch, write_report
    try:
        ep = load_endpoint_config()
    except (EndpointError, PaidEndpointError) as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return 2
    if not os.path.isdir(args.input_dir):
        print("目录不存在：%s" % args.input_dir, file=sys.stderr)
        return 2
    if args.workers > 1:
        print("⚠ 多进程并发已开启（%d workers）：引擎为子进程，请注意内存与限速。" % args.workers)

    def prog(done, total):
        sys.stdout.write("\r[%d/%d]" % (done, total))
        sys.stdout.flush()

    result = run_batch(
        args.input_dir, args.output, ep.base_url, ep.api_key, ep.model,
        workers=args.workers, resume=not args.no_resume,
        blacklist=args.blacklist or [], lang_in=args.lang_in,
        lang_out=args.lang_out, qps=args.qps if args.qps is not None else ep.qps,
        per_file_timeout_s=args.timeout_s, force_tier=args.tier,
        repair=args.repair, no_glossary=args.no_glossary,
        openai_timeout=ep.timeout, part_pages=args.part_pages,
        glossary=args.glossary,
        progress=prog if not args.quiet else None)
    if not args.quiet:
        print()
    report_path = os.path.join(args.output, "report.md")
    write_report(result, report_path)
    print("报告: %s" % report_path)
    print(result.summary())
    return 0 if (result.failed == 0 and result.timeout == 0) else 4


def cmd_report(args) -> int:
    from .runner import write_report, _load_done
    if not os.path.isdir(args.outdir):
        print("目录不存在：%s" % args.outdir, file=sys.stderr)
        return 2
    audit_path = os.path.join(args.outdir, "audit.jsonl")
    done = _load_done(audit_path)
    if not done:
        print("没有可汇总的成功记录（%s）。" % audit_path, file=sys.stderr)
        return 2
    from .runner import BatchResult
    r = BatchResult(outdir=args.outdir, total=len(done), success=len(done))
    for e in done.values():
        r.entries.append(e)
    report_path = os.path.join(args.outdir, "report.md")
    write_report(r, report_path)
    print("报告: %s" % report_path)
    return 0


def cmd_selfcheck(args) -> int:
    checks = []

    def add(name, ok, note=""):
        checks.append((name, bool(ok), note))

    import platform
    add("python>=3.10", sys.version_info >= (3, 10), platform.python_version())
    try:
        import pymupdf  # noqa: F401
        add("pymupdf", True, pymupdf.__version__ if hasattr(pymupdf, "__version__") else "ok")
    except Exception as exc:
        add("pymupdf", False, str(exc))
    from .engine_babeldoc import find_engine_bin, EngineNotFoundError
    try:
        add("pdf2zh_next 引擎", True, find_engine_bin())
    except EngineNotFoundError as exc:
        add("pdf2zh_next 引擎", False, str(exc).splitlines()[0])
    from .ocr_adapter import tesseract_info
    tess = tesseract_info()
    add("tesseract（C级 OCR，可选）", tess.get("available", False),
        tess.get("version", tess.get("reason", "")))
    gpu = "未检测到"
    try:
        import subprocess as sp
        r = sp.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            gpu = r.stdout.strip().splitlines()[0]
    except Exception:
        pass
    add("GPU（可选，加速版面分析）", gpu != "未检测到", gpu)

    ep_note = ""
    ep_ok = False
    try:
        ep = load_endpoint_config()
        ep_ok = True
        ep_note = "%s @ %s" % (ep.model, ep.base_url)
    except (EndpointError, PaidEndpointError) as exc:
        ep_note = str(exc).splitlines()[0]
    add("翻译端点配置", ep_ok, ep_note)

    if args.net and ep_ok:
        try:
            import urllib.request
            req = urllib.request.Request(
                ep.base_url + "/chat/completions",
                data=json.dumps({"model": ep.model, "messages": [
                    {"role": "user", "content": "ping"}], "max_tokens": 1}).encode(),
                headers={"Authorization": "Bearer " + ep.api_key,
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                add("端点连通（--net）", resp.status == 200, "HTTP %s" % resp.status)
        except Exception as exc:
            add("端点连通（--net）", False, str(exc)[:120])

    print()
    all_ok = True
    for name, ok, note in checks:
        mark = "✅" if ok else ("⚠️" if "可选" in name else "❌")
        if not ok and "可选" not in name:
            all_ok = False
        print("%s %-28s %s" % (mark, name, note))
    print("\n汇总: %s" % ("PASS 全部关键项通过" if all_ok else "FAIL 有关键项未通过"))
    return 0 if all_ok else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="doc-holmes",
        description="保持原文排版的外文 PDF 精准翻译（体检分级 A/B/C + BabelDOC 引擎）")
    ap.add_argument("--version", action="version", version="doc-holmes %s" % __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("triage", help="体检分级：A(高保真)/B(有噪)/C(扫描需OCR)")
    p.add_argument("paths", nargs="+", help="PDF 文件或目录")
    p.add_argument("--sample-pages", type=int, default=3, help="采样页数（默认 3）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("translate", help="翻译单个 PDF（输出 双语对照 + 纯译文）")
    p.add_argument("pdf", help="输入 PDF 路径")
    p.add_argument("-o", "--output", help="输出目录（默认输入旁 _translated/）")
    p.add_argument("--lang-in", default="en", help="源语言（默认 en）")
    p.add_argument("--lang-out", default="zh", help="目标语言（默认 zh）")
    p.add_argument("--pages", help="页范围，如 1-5")
    p.add_argument("--tier", choices=["auto", "A", "B", "C"], default="auto",
                   help="强制分级（默认 auto 体检决定）")
    p.add_argument("--ocr", choices=["auto", "off"], default="auto",
                   help="C 级（扫描件）OCR 通道开关（默认 auto）")
    p.add_argument("--ocr-lang", default="eng", help="OCR 语言包（默认 eng，可用 eng+chi_sim）")
    p.add_argument("--no-dual", action="store_true", help="不输出双语对照版")
    p.add_argument("--no-mono", action="store_true", help="不输出纯译文版")
    p.add_argument("--qps", type=int, default=None, help="翻译请求速率（默认 4，防限流）")
    p.add_argument("--timeout-s", type=int, default=None, help="单文件超时秒数")
    p.add_argument("--repair", choices=["auto", "on", "off"], default="auto",
                   help="B 级图层重复检测（只读，任何模式都不改 PDF；auto=仅 B 级；"
                        "on=全文档强制含 A 级；off=关闭）")
    p.add_argument("--no-glossary", action="store_true",
                   help="关闭自动术语抽取（大文档该阶段可能超时；术语一致性会略降）")
    p.add_argument("--part-pages", type=int, default=None,
                   help="分段翻译每段页数（0=不分段；默认自动：≥40 页按 25 页/段）")
    p.add_argument("--glossary", choices=["auto", "off"], default="auto",
                   help="术语抽取策略（auto=大文档自动跳过；off=始终跳过）")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("batch", help="批量翻译目录下全部 PDF（断点续传 + 审计）")
    p.add_argument("input_dir", help="输入目录（递归收集 PDF）")
    p.add_argument("-o", "--output", required=True, help="输出目录")
    p.add_argument("--workers", type=int, default=1, help="并发数 1-4（默认 1）")
    p.add_argument("--no-resume", action="store_true", help="忽略历史审计强制重跑")
    p.add_argument("--blacklist", nargs="*", default=[], help="黑名单文件路径列表")
    p.add_argument("--lang-in", default="en")
    p.add_argument("--lang-out", default="zh")
    p.add_argument("--qps", type=int, default=None)
    p.add_argument("--tier", choices=["auto", "A", "B", "C"], default="auto")
    p.add_argument("--timeout-s", type=int, default=None)
    p.add_argument("--repair", choices=["auto", "on", "off"], default="auto",
                   help="B 级图层重复检测（只读；默认 auto，on=全文档强制）")
    p.add_argument("--no-glossary", action="store_true",
                   help="关闭自动术语抽取（大文档防超时）")
    p.add_argument("--part-pages", type=int, default=None,
                   help="分段翻译每段页数（默认自动：≥40 页按 25 页/段）")
    p.add_argument("--glossary", choices=["auto", "off"], default="auto")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("report", help="从 batch 输出目录汇总 report.md")
    p.add_argument("outdir", help="batch 的输出目录（含 audit.jsonl）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("selfcheck", help="环境自检：依赖/引擎/OCR/GPU/端点")
    p.add_argument("--net", action="store_true", help="附带真实端点连通性探测")
    p.set_defaults(func=cmd_selfcheck)
    return ap


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
