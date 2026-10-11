"""批量与定时处理：扫描收件目录、按内容指纹去重、可选归档与导出.

`watch` 子命令用轮询实现「定时任务」——刻意不引入 APScheduler 之类的调度依赖：
把「什么时候跑」交给操作系统（cron / Windows 计划任务），把「跑什么、跳过什么」交给这里。
这样部署方式最灵活，也少一个需要维护的运行时组件。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from docflow.config import Settings
from docflow.errors import DocFlowError
from docflow.export import export_auto
from docflow.models.schema import SchemaDefinition
from docflow.pipeline import PipelineSpec, run_pipeline
from docflow.pipeline.report import PipelineResult


def collect_files(directory: Path, patterns: Sequence[str] = ("*",), recursive: bool = False) -> list[Path]:
    """按通配符收集目录下的文件；去重并排序，保证处理顺序稳定可预期."""
    if not directory.exists():
        return []
    found: set[Path] = set()
    for pattern in patterns:
        matches = directory.rglob(pattern) if recursive else directory.glob(pattern)
        found.update(path for path in matches if path.is_file())
    return sorted(found)


def file_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """文件内容 SHA-256：同一份文件改名/换目录后仍能识别为「已处理过」."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProcessedLedger:
    """已处理文件账本：指纹 → 处理信息，持久化成 JSON，重启后仍能去重."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._entries = {}  # 账本损坏时当作空账本，宁可重跑也不阻断批处理
            return
        if isinstance(data, dict):
            self._entries = {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def seen(self, fingerprint: str) -> bool:
        return fingerprint in self._entries

    def record(self, fingerprint: str, file_name: str, run_id: str) -> None:
        self._entries[fingerprint] = {
            "file": file_name,
            "run_id": run_id,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class BatchReport:
    """一轮批处理的结果摘要."""

    candidates: list[Path] = field(default_factory=list)  # 收件目录里发现的文件
    processed: list[Path] = field(default_factory=list)  # 本轮实际处理成功的文件
    skipped: list[Path] = field(default_factory=list)  # 命中账本、跳过不重复处理的文件
    failed: list[Path] = field(default_factory=list)  # 解析失败、留在原地待重试的文件
    result: PipelineResult | None = None
    export_path: Path | None = None
    error: str | None = None

    @property
    def has_work(self) -> bool:
        return bool(self.processed or self.failed)


class BatchProcessor:
    """收件目录处理器：一轮 = 扫描 → 去重 → 处理 → 落库 → 归档 / 导出."""

    def __init__(
        self,
        schema: SchemaDefinition,
        settings: Settings,
        ledger_path: Path | None = None,
        spec: PipelineSpec | None = None,
        checkpoint_dir: Path | None = None,
    ) -> None:
        self.schema = schema
        self.settings = settings
        self.spec = spec
        self.checkpoint_dir = checkpoint_dir
        self.ledger = ProcessedLedger(ledger_path or (settings.data_dir / "state" / "processed.json"))

    def process(
        self,
        inbox: Path,
        patterns: Sequence[str] = ("*",),
        recursive: bool = False,
        archive: Path | None = None,
        export: Path | None = None,
        as_csv: bool = False,
        reprocess: bool = False,
    ) -> BatchReport:
        """处理收件目录里所有「尚未处理」的文件.

        去重同时覆盖**账本**（跨轮次）与**本批次**：同一轮里两份内容相同的文件
        （例如 a.csv 与它的改名副本）只处理一份，另一份按「已处理」跳过——
        否则它们会在写账本之前同时进入待处理列表，导致重复数据入库。

        只把**成功解析**的文件写进账本并归档；解析失败的文件留在原地，下一轮自动重试。
        """
        report = BatchReport(candidates=collect_files(inbox, patterns, recursive))
        if not report.candidates:
            return report

        fresh: list[Path] = []
        seen_this_round: set[str] = set()
        for path in report.candidates:
            try:
                fingerprint = file_fingerprint(path)
            except OSError as exc:
                report.failed.append(path)
                report.error = f"{path.name} 不可读：{exc}"
                continue
            if not reprocess and (self.ledger.seen(fingerprint) or fingerprint in seen_this_round):
                report.skipped.append(path)
                continue
            seen_this_round.add(fingerprint)
            fresh.append(path)

        if not fresh:
            return report

        try:
            result = run_pipeline(self.schema, self.settings, fresh, spec=self.spec, checkpoint_dir=self.checkpoint_dir)
        except DocFlowError as exc:
            report.failed.extend(fresh)
            report.error = str(exc)
            return report

        report.result = result
        failed_names = {failure.file_name for failure in result.failures}
        for path in fresh:
            if path.name in failed_names:
                report.failed.append(path)
            else:
                report.processed.append(path)

        from docflow.store import build_session_factory, save_run

        factory = build_session_factory(self.settings)
        with factory() as session:
            save_run(session, result)
            session.commit()

        for path in report.processed:
            self.ledger.record(file_fingerprint(path), path.name, result.run_id)
        self.ledger.save()

        if archive is not None and report.processed:
            self._archive(report.processed, archive)

        if export is not None:
            target = export / result.run_id if export.suffix == "" else export
            report.export_path = export_auto(result, target, self.schema, as_csv=as_csv)

        return report

    @staticmethod
    def _archive(paths: list[Path], archive: Path) -> None:
        archive.mkdir(parents=True, exist_ok=True)
        for path in paths:
            destination = archive / path.name
            if destination.exists():  # 同名文件加时间戳，避免覆盖既有归档
                stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
                destination = archive / f"{path.stem}_{stamp}{path.suffix}"
            shutil.move(str(path), str(destination))
