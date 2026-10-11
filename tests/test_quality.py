"""质检规则：格式、取值区间、整列为空等非阻断型问题."""

from __future__ import annotations

from docflow.models.record import IssueCode, ReviewStatus
from docflow.pipeline import run_pipeline


def test_pattern_mismatch_routes_to_review(settings, purchase_order, write_csv):
    path = write_csv(
        "bad_order_no.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期\nXYZ,华信机械,A001,螺栓,10,1,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert any(issue.code is IssueCode.PATTERN_MISMATCH for issue in record.issues)
    # 格式告警虽不阻断，但必须让记录停下来等人确认，不能因为置信度高就自动放行
    assert record.review_status is ReviewStatus.PENDING
    assert result.review_pending == 1


def test_range_violation_is_reported(settings, purchase_order, write_csv):
    path = write_csv(
        "zero_quantity.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期\nPO-2024-001,华信机械,A001,螺栓,0,1,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    assert any(issue.code is IssueCode.RANGE_VIOLATION for issue in result.records[0].issues)


def test_all_blank_column_is_flagged(settings, purchase_order, write_csv):
    path = write_csv(
        "blank_amount.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期\n"
        "PO-2024-001,华信机械,A001,螺栓,10,1,,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    assert any(
        issue.code is IssueCode.EMPTY_COLUMN and issue.field == "total_amount"
        for issue in result.sources[0].table_issues
    )


def test_formula_mismatch_is_flagged(settings, purchase_order, write_csv):
    """总金额与「数量 × 单价」对不上时，必须报 CROSS_FIELD 并让记录停下来."""
    path = write_csv(
        "bad_total.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期\n"
        "PO-2024-001,华信机械,A001,螺栓,10,2,999,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    issue = next(i for i in record.issues if i.code is IssueCode.CROSS_FIELD and i.field == "total_amount")
    assert "总金额" in issue.message
    assert record.review_status is ReviewStatus.PENDING


def test_formula_within_tolerance_passes(settings, purchase_order, write_csv):
    """小数舍入导致的极小偏差不应误报：99.999 与 100 在 1% 容差内视为一致."""
    path = write_csv(
        "rounding_total.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期\n"
        "PO-2024-001,华信机械,A001,螺栓,3,33.333,99.999,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert not any(i.code is IssueCode.CROSS_FIELD and i.field == "total_amount" for i in record.issues)
    assert record.review_status is ReviewStatus.AUTO_APPROVED


def test_formula_skips_when_operand_missing(settings, purchase_order, write_csv):
    """数量缺值时不算「算错账」：交给必填校验，公式不应重复报错."""
    path = write_csv(
        "missing_operand.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期\n"
        "PO-2024-001,华信机械,A001,螺栓,,2,50,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert not any(i.code is IssueCode.CROSS_FIELD and i.field == "total_amount" for i in record.issues)
    assert any(i.code is IssueCode.MISSING_REQUIRED and i.field == "quantity" for i in record.issues)
