"""置信度分流：决定每条记录是自动通过还是进人工复核队列.

三级策略（阈值全部可配）：
- 有 ERROR 级问题            → 强制复核
- min_confidence < review    → 强制复核
- 有 WARNING 级问题          → 建议检查（进队列，等人工确认；即便置信度很高也不自动放行）
- review ≤ conf < auto       → 建议检查
- conf ≥ auto 且无 ERROR/WARNING → 自动通过

「有 WARNING 就不自动放行」是关键修正：格式不符、超出范围这类警告过去不参与分流，
高置信度的脏记录会被直接判为自动通过、悄悄入库。既然人工复核队列存在的意义就是兜住这类
「机器拿不准」的情况，警告级问题就必须让记录停下来等人看一眼。
"""

from __future__ import annotations

from docflow.config import Settings
from docflow.models.record import ReviewStatus, UnifiedRecord

HINT_AUTO = "自动通过"
HINT_SUGGEST = "建议检查"
HINT_MANDATORY = "强制复核"


def decide_status(record: UnifiedRecord, settings: Settings) -> ReviewStatus:
    if record.has_error:
        return ReviewStatus.PENDING
    if record.min_confidence < settings.review_threshold:
        return ReviewStatus.PENDING
    if record.has_warning:
        return ReviewStatus.PENDING
    if record.min_confidence >= settings.auto_approve_threshold:
        return ReviewStatus.AUTO_APPROVED
    return ReviewStatus.PENDING


def review_hint(record: UnifiedRecord, settings: Settings) -> str:
    """给用户看的分流结论。比 ReviewStatus 多一档「建议检查」，便于前端排序."""
    if record.has_error or record.min_confidence < settings.review_threshold:
        return HINT_MANDATORY
    if record.has_warning or record.min_confidence < settings.auto_approve_threshold:
        return HINT_SUGGEST
    return HINT_AUTO


def is_publishable(record: UnifiedRecord) -> bool:
    """能否进入下游业务系统：自动通过或人工已确认."""
    return record.review_status in {ReviewStatus.AUTO_APPROVED, ReviewStatus.APPROVED}


def assign_review_status(records: list[UnifiedRecord], settings: Settings) -> None:
    """就地写入 review_status。人工已处理的记录（APPROVED/REJECTED）不覆盖."""
    for record in records:
        if record.review_status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}:
            continue
        record.review_status = decide_status(record, settings)
