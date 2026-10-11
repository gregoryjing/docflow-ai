"""管线运行的中间状态：步骤之间传递的可序列化上下文.

把状态显式建模成 Pydantic 模型，是为了让「步骤级断点续跑」有一个清晰的落盘边界——
如果状态散落在闭包与局部变量里，就没法在任意一步之后把进度存下来。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from docflow.models.dedupe import DedupeResult
from docflow.models.mapping import MappingResult
from docflow.models.record import QualityIssue, UnifiedRecord
from docflow.models.table import RawTable
from docflow.pipeline.report import FileFailure, FileWarning


class TableContext(BaseModel):
    """一张源表的处理上下文：映射 → 记录 → 表级问题，随步骤推进逐步填充."""

    table: RawTable
    mapping: MappingResult | None = None
    records: list[UnifiedRecord] = Field(default_factory=list)
    table_issues: list[QualityIssue] = Field(default_factory=list)


class PipelineState(BaseModel):
    """一条管线从接入到去重的全部中间产物."""

    paths: list[Path] = Field(default_factory=list, description="本次输入文件，ingest 步骤消费")
    tables: list[RawTable] = Field(default_factory=list)
    failures: list[FileFailure] = Field(default_factory=list)
    warnings: list[FileWarning] = Field(default_factory=list)
    contexts: list[TableContext] = Field(default_factory=list)
    dedupe: DedupeResult = Field(default_factory=DedupeResult)

    @property
    def records(self) -> list[UnifiedRecord]:
        """把所有来源的记录拍平——一致性、分流、去重都在这个扁平视图上操作."""
        return [record for context in self.contexts for record in context.records]
