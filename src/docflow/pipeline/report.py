"""管线运行结果模型：一次处理跑完之后，调用方需要看到的所有东西都在这里."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from docflow.models.dedupe import DedupeResult
from docflow.models.mapping import MappingResult
from docflow.models.record import IssueLevel, QualityIssue, ReviewStatus, UnifiedRecord
from docflow.models.table import SourceRef


class FileFailure(BaseModel):
    """连解析都没过去的文件——单独列出，不与「处理成功但有质量问题」混淆."""

    file_name: str
    error: str


class FileWarning(BaseModel):
    """解析成功却零产出的文件：不阻断整批，但必须让用户看到「它为什么没进来」."""

    file_name: str
    message: str


class SourceOutcome(BaseModel):
    """单个来源（一个 Sheet / 一页 / 一张表）的处理结果摘要."""

    source: SourceRef
    mapping: MappingResult
    record_count: int = 0
    review_pending: int = 0
    table_issues: list[QualityIssue] = Field(default_factory=list)
    record_issue_levels: dict[str, int] = Field(
        default_factory=dict, description="该来源下记录级问题按等级计数（error/warning/info）"
    )

    @property
    def issue_counts(self) -> dict[str, int]:
        counts = {level.value: 0 for level in IssueLevel}
        for issue in self.table_issues:
            counts[issue.level.value] += 1
        return counts

    @property
    def status(self) -> str:
        # 来源状态必须同时看表级与记录级：只看表级会让「有错误记录、但表级干净」的
        # 来源显示成绿色 ok，用户据此以为这个来源没问题——而记录其实已经进了复核队列。
        has_error = any(issue.level is IssueLevel.ERROR for issue in self.table_issues) or self.record_issue_levels.get(
            IssueLevel.ERROR.value, 0
        )
        if has_error:
            return "error"
        if self.table_issues or self.record_issue_levels.get(IssueLevel.WARNING.value, 0):
            return "warning"
        return "ok"


class PipelineResult(BaseModel):
    run_id: str
    schema_name: str
    schema_label: str = ""
    started_at: datetime
    finished_at: datetime
    records: list[UnifiedRecord] = Field(default_factory=list)
    sources: list[SourceOutcome] = Field(default_factory=list)
    failures: list[FileFailure] = Field(default_factory=list)
    file_warnings: list[FileWarning] = Field(
        default_factory=list, description="解析成功但未产出数据的文件（如扫描件未开 OCR、空文件）"
    )
    dedupe: DedupeResult = Field(default_factory=DedupeResult, description="去重扫描结果（只报告，不删数据）")

    # ── 管线执行轨迹（可观测性）────────────────────────────────
    pipeline_name: str = Field(default="default", description="本次使用的 Pipeline 规范名")
    executed_steps: list[str] = Field(default_factory=list, description="本次实际执行的步骤序列")
    resumed_from: str | None = Field(default=None, description="断点续跑时，从哪一步之后恢复（未续跑为空）")

    # ── 汇总指标 ──────────────────────────────────────────────
    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def total_records(self) -> int:
        return len(self.records)

    @property
    def issue_counts(self) -> dict[str, int]:
        """记录级 + 表级问题按等级汇总."""
        counts = {level.value: 0 for level in IssueLevel}
        for record in self.records:
            for issue in record.issues:
                counts[issue.level.value] += 1
        for source in self.sources:
            for issue in source.table_issues:
                counts[issue.level.value] += 1
        return counts

    @property
    def issue_code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            for issue in record.issues:
                counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
        for source in self.sources:
            for issue in source.table_issues:
                counts[issue.code.value] = counts.get(issue.code.value, 0) + 1
        return counts

    @property
    def review_pending(self) -> int:
        return sum(1 for record in self.records if record.review_status is ReviewStatus.PENDING)

    @property
    def auto_approved(self) -> int:
        return sum(1 for record in self.records if record.review_status is ReviewStatus.AUTO_APPROVED)

    @property
    def duplicate_groups(self) -> int:
        return len(self.dedupe.groups)

    @property
    def duplicate_records(self) -> int:
        """冗余记录数：重复组里除保留的那条之外的记录."""
        return self.dedupe.duplicate_record_count

    @property
    def error_count(self) -> int:
        return self.issue_counts[IssueLevel.ERROR.value]

    @property
    def warning_count(self) -> int:
        return self.issue_counts[IssueLevel.WARNING.value]
