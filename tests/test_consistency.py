"""跨来源一致性校验：同一实体在多份来源里的字段冲突（四层质检体系的最外层）."""

from __future__ import annotations

from docflow.models.record import IssueCode, IssueLevel, ReviewStatus
from docflow.models.schema import ConsistencyRule
from docflow.pipeline import run_pipeline

HEADER = "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期,交付日期,是否加急\n"


def _row(order_no: str, supplier: str, material_name: str = "螺栓") -> str:
    return f"{order_no},{supplier},A001,{material_name},100,0.5,50,2024-03-01,2024-03-15,否\n"


def _cross_source(result) -> list:
    return [issue for record in result.records for issue in record.issues if issue.code is IssueCode.CROSS_SOURCE]


def test_cross_source_conflict_is_flagged(settings, purchase_order, write_csv):
    ledger = write_csv("ledger.csv", HEADER + _row("PO-2024-001", "华信机械"))
    receipt = write_csv("receipt.csv", HEADER + _row("PO-2024-001", "远大五金"))

    result = run_pipeline(purchase_order, settings, [ledger, receipt])

    conflicts = _cross_source(result)
    assert conflicts
    assert {issue.field for issue in conflicts} == {"supplier"}
    assert all(issue.level is IssueLevel.WARNING for issue in conflicts)


def test_consistent_multi_source_has_no_conflict(settings, purchase_order, write_csv):
    first = write_csv("a.csv", HEADER + _row("PO-2024-001", "华信机械"))
    second = write_csv("b.csv", HEADER + _row("PO-2024-001", "华信机械"))

    result = run_pipeline(purchase_order, settings, [first, second])

    assert not _cross_source(result)


def test_single_source_duplicate_is_not_cross_source(settings, purchase_order, write_csv):
    """同一文件内的主键重复属于表级问题，不应误报成跨源冲突."""
    path = write_csv("dup.csv", HEADER + _row("PO-2024-001", "华信机械") + _row("PO-2024-001", "远大五金"))

    result = run_pipeline(purchase_order, settings, [path])

    assert not _cross_source(result)
    assert any(issue.code is IssueCode.DUPLICATE_KEY for issue in result.sources[0].table_issues)


def test_error_level_consistency_forces_review(settings, purchase_order, write_csv):
    strict = purchase_order.model_copy(
        update={
            "consistency": [ConsistencyRule(key="order_no", fields=["supplier"], level=IssueLevel.ERROR)],
        }
    )
    first = write_csv("a.csv", HEADER + _row("PO-2024-001", "华信机械"))
    second = write_csv("b.csv", HEADER + _row("PO-2024-001", "远大五金"))

    result = run_pipeline(strict, settings, [first, second])

    assert any(issue.level is IssueLevel.ERROR for issue in _cross_source(result))
    assert all(record.review_status is ReviewStatus.PENDING for record in result.records)
