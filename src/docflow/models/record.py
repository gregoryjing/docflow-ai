from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class IssueLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class IssueCode(StrEnum):
    """质量问题编码。前端按 code 分类展示，导出报告按 code 聚合统计."""

    MISSING_REQUIRED = "missing_required"  # 必填字段为空
    TYPE_MISMATCH = "type_mismatch"  # 类型转换失败
    PATTERN_MISMATCH = "pattern_mismatch"  # 正则不匹配
    RANGE_VIOLATION = "range_violation"  # 超出取值范围
    DUPLICATE_KEY = "duplicate_key"  # 主键重复
    DUPLICATE_ROW = "duplicate_row"  # 整行重复
    EMPTY_COLUMN = "empty_column"  # 整列为空
    CROSS_FIELD = "cross_field"  # 跨字段逻辑冲突
    LOW_CONFIDENCE = "low_confidence"  # 置信度偏低
    UNMAPPED_COLUMN = "unmapped_column"  # 源列未映射到任何标准字段
    MISSING_FIELD = "missing_field"  # Schema 必填字段在源文件中完全缺失
    CROSS_SOURCE = "cross_source"  # 同一实体在不同来源中的字段取值不一致
    DUPLICATE_ENTITY = "duplicate_entity"  # 主键相同，指向同一实体（精确重复）
    POSSIBLE_DUPLICATE = "possible_duplicate"  # 主键缺失但多字段高度相似（疑似重复，需人工确认）


class ReviewStatus(StrEnum):
    AUTO_APPROVED = "auto_approved"  # 置信度足够，自动通过
    PENDING = "pending"  # 待人工复核
    APPROVED = "approved"  # 人工确认通过
    REJECTED = "rejected"  # 人工驳回


class QualityIssue(BaseModel):
    """一条质量问题，带定位信息与解释，可直接渲染给用户看."""

    level: IssueLevel
    code: IssueCode
    message: str
    field: str | None = None
    row_index: int | None = Field(default=None, description="记录在解析后表中的行号（0 起）")
    value: Any = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FieldValue(BaseModel):
    """一个标准字段的取值，附带来源与置信度，保证可追溯."""

    value: Any = None
    raw: Any = Field(default=None, description="标准化前的原始值，用于溯源与人工复核")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_column: str | None = Field(default=None, description="映射来源的原始列名")
    note: str = ""


class UnifiedRecord(BaseModel):
    """统一记录模型：所有来源、所有格式的数据最终都收敛成这个结构."""

    record_id: str
    schema_name: str
    source_file: str
    source_location: str = ""
    row_index: int | None = None
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    issues: list[QualityIssue] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.AUTO_APPROVED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def min_confidence(self) -> float:
        if not self.fields:
            return 0.0
        return min(field.confidence for field in self.fields.values())

    @property
    def has_error(self) -> bool:
        return any(issue.level == IssueLevel.ERROR for issue in self.issues)

    @property
    def has_warning(self) -> bool:
        return any(issue.level == IssueLevel.WARNING for issue in self.issues)

    def get(self, key: str) -> Any:
        field = self.fields.get(key)
        return None if field is None else field.value

    def to_flat(self) -> dict[str, Any]:
        """导出用：只保留标准字段值，丢掉置信度等元信息."""
        return {key: field.value for key, field in self.fields.items()}
