"""去重结果模型：只描述「哪些记录指向同一实体」，不承担删除/改写.

设计取舍：检测与合并分离。管线只负责**发现**重复并给出分组，
是否真的合并、合并后保留哪条，交给显式动作或人工决定——
企业场景里「静默合并」是不可接受的，因为合并错误无法轻易回滚。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DuplicateMode(StrEnum):
    EXACT = "exact"  # 主键完全相同
    FUZZY = "fuzzy"  # 主键缺失/不可靠时，多字段相似度超过阈值


class DuplicateGroup(BaseModel):
    """一组指向同一实体的记录."""

    key_value: str = Field(description="实体标识的可读展示，如主键值或关键字段拼接")
    mode: DuplicateMode
    record_ids: list[str]
    similarity: float = Field(default=1.0, ge=0.0, le=1.0, description="组内最弱一环的相似度，精确重复为 1.0")
    review_required: bool = Field(default=False, description="是否必须人工确认（模糊匹配一律为真）")
    detail: str = ""

    @property
    def size(self) -> int:
        return len(self.record_ids)


class DedupeResult(BaseModel):
    """一次去重扫描的结果."""

    groups: list[DuplicateGroup] = Field(default_factory=list)

    @property
    def exact_groups(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.mode is DuplicateMode.EXACT]

    @property
    def fuzzy_groups(self) -> list[DuplicateGroup]:
        return [group for group in self.groups if group.mode is DuplicateMode.FUZZY]

    @property
    def duplicate_record_count(self) -> int:
        """冗余记录数：每组保留一条，其余都是重复项."""
        return sum(group.size - 1 for group in self.groups)

    @property
    def review_required_count(self) -> int:
        """需要人工确认的记录数（模糊组全部计入）."""
        return sum(group.size for group in self.groups if group.review_required)
