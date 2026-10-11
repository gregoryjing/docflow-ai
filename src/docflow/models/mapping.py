from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from docflow.models.table import SourceRef


class MappingMethod(StrEnum):
    """字段是怎么映射上的——决定可解释性，也决定要不要进人工复核."""

    EXACT = "exact"  # 归一化后与字段名/同义词完全一致
    SYNONYM = "synonym"  # 命中同义词词典
    FUZZY = "fuzzy"  # 模糊匹配
    LLM = "llm"  # LLM 推断（M2）
    MANUAL = "manual"  # 人工指定
    NONE = "none"  # 未映射


class ColumnMapping(BaseModel):
    """一列的映射结论，含置信度与理由，前端直接展示这张表让用户确认."""

    source_column: str
    target_field: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    method: MappingMethod = MappingMethod.NONE
    reason: str = ""
    alternatives: list[tuple[str, float]] = Field(
        default_factory=list, description="次优候选（字段, 得分），供人工复核时切换"
    )

    @property
    def is_mapped(self) -> bool:
        return self.target_field is not None


class MappingResult(BaseModel):
    """一份源表的映射结果：列级映射 + 缺失的必填字段."""

    source: SourceRef
    schema_name: str
    columns: list[ColumnMapping] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)

    def mapped_columns(self) -> list[ColumnMapping]:
        return [column for column in self.columns if column.is_mapped]

    def unmapped_columns(self) -> list[ColumnMapping]:
        return [column for column in self.columns if not column.is_mapped]

    def needs_review(self, suggest_threshold: float) -> list[ColumnMapping]:
        """置信度落在 [suggest, accept) 或未映射的列，需要人工确认."""
        return [column for column in self.columns if column.confidence < suggest_threshold]
