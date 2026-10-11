"""质检层：字段级 / 记录级 / 表级 / 跨源级规则 + 去重 + 置信度分流."""

from docflow.quality.consistency import CrossSourceChecker
from docflow.quality.dedupe import Deduplicator
from docflow.quality.engine import QualityEngine
from docflow.quality.status import (
    HINT_AUTO,
    HINT_MANDATORY,
    HINT_SUGGEST,
    assign_review_status,
    decide_status,
    is_publishable,
    review_hint,
)

__all__ = [
    "HINT_AUTO",
    "HINT_MANDATORY",
    "HINT_SUGGEST",
    "CrossSourceChecker",
    "Deduplicator",
    "QualityEngine",
    "assign_review_status",
    "decide_status",
    "is_publishable",
    "review_hint",
]
