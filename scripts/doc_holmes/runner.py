"""批量 runner：工程铁律逐条落实（plan.md T4.1，每条对应一个测试）。

① 路径一律 os.path.realpath 后比较/存储
② os.walk 显式排除：_duplicates/ _non_pdf_assets/ __pycache__/ .git/ 及输出目录
③ 黑名单/完成表用 set
④ 默认单进程；--workers N（N≤4）显式开启并警告
⑤ 每文件超时（engine 层强制）
⑥ watchdog：跟踪最近完成时刻，停滞超阈值的在跑文件记审计 stalled_timeout
⑦ 断点续传：audit.jsonl 重放，已完成且产物仍在 → skip
⑧ python -m compileall 进测试链（tests/test_runner.py）
审计状态：success / failed / timeout / skipped（failed 含产物回滚）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import triage as triage_mod
from .engine_babeldoc import discover_outputs, translate_pdf

EXCLUDE_DIRS = {"_duplicates", "_non_pdf_assets", "__pycache__", ".git",
                "output", "_translated", ".venv", "node_modules"}
AUDIT_NAME = "audit.jsonl"
WATCHDOG_STALL_S = 300


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_pdfs(root: str) -> list:
    """铁律①②：realpath 收集 + 排除目录。"""
    real_root = os.path.realpath(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(real_root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for name in filenames:
            if name.lower().endswith(".pdf"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


@dataclass
class BatchResult:
    outdir: str
    total: int = 0
    success: int = 0
    failed: int = 0
    timeout: int = 0
    skipped: int = 0
    entries: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def summary(self) -> str:
        return ("汇总: 总计 %d | 成功 %d | 失败 %d | 超时 %d | 跳过(已完成) %d → %s"
                % (self.total, self.success, self.failed, self.timeout,
                   self.skipped, self.outdir))


def _load_done(audit_path: str) -> dict:
    """铁律⑦：重放审计日志。key=sha256 → entry（仅 success）。"""
    done = {}
    if not os.path.isfile(audit_path):
        return done
    with open(audit_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("status") == "success":
                done[entry.get("sha256")] = entry
    return done


def _outputs_intact(entry: dict) -> bool:
    for key in ("dual_path", "mono_path"):
        p = entry.get(key)
        if p and not os.path.isfile(p):
            return False
    return bool(entry.get("dual_path") or entry.get("mono_path"))


class Watchdog:
    """铁律⑥：停滞监测（记录；杀进程由 engine 的 per-file 超时兜底）。"""

    def __init__(self, stall_s: int = WATCHDOG_STALL_S):
        self.stall_s = stall_s
        self.last_progress = time.time()
        self.current = None
        self.stalled = []
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def touch(self, name=None):
        with self._lock:
            self.last_progress = time.time()
            self.current = name

    def start(self):
        def loop():
            while not self._stop.wait(30):
                with self._lock:
                    idle = time.time() - self.last_progress
                    if idle > self.stall_s and self.current:
                        self.stalled.append({"file": self.current, "stall_s": round(idle)})
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return self

    def stop(self):
        self._stop.set()


def run_batch(input_dir: str, outdir: str, base_url: str, api_key: str, model: str,
              *, workers: int = 1, resume: bool = True, blacklist=None,
              lang_in: str = "en", lang_out: str = "zh", qps: int = 4,
              per_file_timeout_s: int = None, do_triage: bool = True,
              force_tier: str = None, repair: str = "auto",
              no_glossary: bool = False, openai_timeout: int = None,
              progress=None) -> BatchResult:
    """批量翻译。审计逐文件落 outdir/audit.jsonl；失败文件产物回滚。"""
    if workers > 4:
        raise ValueError("workers 上限 4（子进程并发过高会拖垮宿主机）")
    os.makedirs(outdir, exist_ok=True)
    audit_path = os.path.join(outdir, AUDIT_NAME)
    done = _load_done(audit_path) if resume else {}
    blacklist = {os.path.realpath(x) for x in (blacklist or [])}

    pdfs = list_pdfs(input_dir)
    result = BatchResult(outdir=os.path.realpath(outdir), total=len(pdfs),
                         started_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    dog = Watchdog().start()

    def _append_audit(entry):
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _one(pdf: str) -> dict:
        real = os.path.realpath(pdf)
        digest = sha256_of(real)
        rel = os.path.relpath(real, os.path.realpath(input_dir))
        if real in blacklist:
            entry = {"path": rel, "sha256": digest, "status": "skipped",
                     "reason": "blacklist"}
            return entry
        prev = done.get(digest)
        if prev and _outputs_intact(prev):
            entry = {"path": rel, "sha256": digest, "status": "skipped",
                     "reason": "already_done(resume)"}
            return entry

        rep = triage_mod.triage_pdf(real) if do_triage else None
        tier = force_tier or (rep.tier if rep else "?")

        # v1.0 批量通路不自动 OCR：C 级显式跳过并给指引（单文件 translate 支持 --ocr auto）
        if tier == "C":
            return {"path": rel, "sha256": digest, "tier": "C", "status": "skipped",
                    "reason": "tier_C_needs_ocr: 扫描件请用单文件 translate --ocr auto"
                              "（批量 OCR 通道在路线图）"}

        # 每文件独立子目录：并发/回滚互不波及（评审 P0-2）
        file_out = os.path.join(outdir, digest[:12])
        os.makedirs(file_out, exist_ok=True)

        # B 级图层去重修复（plan Phase 2）：auto=B 级自动，off 关闭
        repair_stats = None
        if repair != "off" and (repair == "on" or tier == "B"):
            try:
                from .repair_pdf import inspect_pdf_dedup
                repair_stats = inspect_pdf_dedup(real)
            except Exception as exc:
                repair_stats = {"error": str(exc)[:200]}

        # 铁律⑤：engine 层 per-file 超时
        extra = ["--no-auto-extract-glossary"] if no_glossary else None
        eng = translate_pdf(
            real, file_out, base_url, api_key, model,
            page_count=rep.page_count if rep else None,
            timeout_s=per_file_timeout_s,
            lang_in=lang_in, lang_out=lang_out, qps=qps, extra_args=extra,
            openai_timeout=openai_timeout)


        entry = {
            "path": rel, "sha256": digest, "tier": tier,
            "status": "success" if eng.ok else ("timeout" if eng.timed_out else "failed"),
            "duration_s": round(eng.duration_s, 1),
            "dual_path": eng.dual_path, "mono_path": eng.mono_path,
            "chars_per_page": rep.chars_per_page if rep else None,
            "triage_reasons": rep.reasons if rep else [],
            "repair": repair_stats,
            "error": None if eng.ok else eng.stderr_tail[-600:],
        }
        if not eng.ok:
            # 回滚：删除本次产生的残缺产物（铁律：失败不留半成品）
            for p in (eng.dual_path, eng.mono_path):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            entry["dual_path"] = entry["mono_path"] = None
            entry["rolled_back"] = True
        return entry

    def _one_safe(pdf):
        """单文件异常隔离：任何异常只废该文件，不炸整个批次（评审 P1-3）。"""
        try:
            return _one(pdf)
        except Exception as exc:
            real = os.path.realpath(pdf)
            rel = os.path.relpath(real, os.path.realpath(input_dir))
            return {"path": rel, "sha256": None, "status": "failed",
                    "error": "unhandled: %r" % exc}

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_one_safe, pdf): pdf for pdf in pdfs}
            for fut, pdf in futures.items():
                entry = fut.result()
                _append_audit(entry)
                result.entries.append(entry)
                status = entry["status"]
                if status == "success":
                    result.success += 1
                elif status == "timeout":
                    result.timeout += 1
                elif status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                dog.touch(os.path.basename(pdf))
                if progress:
                    progress(result.success + result.failed + result.timeout
                             + result.skipped, result.total)
    finally:
        dog.stop()
        if dog.stalled:
            result.entries.extend({"watchdog": s} for s in dog.stalled)

    result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return result


def write_report(result: BatchResult, report_path: str) -> str:
    """汇总 report.md：指标 + 逐文件状态表。末行含「汇总」字样（验收链依赖）。"""
    lines = ["# doc-holmes 批量翻译报告", "",
             "- 输入总计: %d" % result.total,
             "- 成功 / 失败 / 超时 / 跳过: %d / %d / %d / %d"
             % (result.success, result.failed, result.timeout, result.skipped),
             "- 时间: %s → %s" % (result.started_at, result.finished_at),
             "", "| 文件 | 级别 | 状态 | 耗时s | 备注 |", "|---|---|---|---|---|"]
    for e in result.entries:
        if "watchdog" in e:
            lines.append("| (watchdog) | - | stalled | %s | %s |"
                         % (e["watchdog"]["stall_s"], e["watchdog"]["file"]))
            continue
        note = e.get("error") or e.get("reason") or ""
        note = str(note).replace("|", "/")[:80]
        lines.append("| %s | %s | %s | %s | %s |"
                     % (e.get("path", "?"), e.get("tier", "-"),
                        e.get("status", "?"), e.get("duration_s", "-"), note))
    lines += ["", result.summary(), ""]
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return report_path
