"""接入 → 映射 → 质检 → 分流 的端到端测试（纯内存，无外部依赖）."""

from __future__ import annotations

from docflow.models.record import IssueCode, ReviewStatus
from docflow.pipeline import run_pipeline


def test_end_to_end_clean_csv(settings, purchase_order, order_csv):
    result = run_pipeline(purchase_order, settings, [order_csv])

    assert not result.failures
    assert result.total_records == 2
    assert result.sources[0].record_count == 2
    assert result.sources[0].mapping.missing_required == []

    first = result.records[0]
    assert first.get("order_no") == "PO-2024-001"
    assert first.get("supplier") == "华信机械"
    assert first.get("quantity") == 100
    assert first.get("unit_price") == 0.5
    assert first.get("total_amount") == 50.0
    assert first.get("order_date") == "2024-03-01"
    assert first.min_confidence == 1.0
    assert first.review_status is ReviewStatus.AUTO_APPROVED


def test_unparseable_value_drops_confidence_and_goes_to_review(settings, purchase_order, write_csv):
    path = write_csv(
        "bad_quantity.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期\nPO-2024-001,华信机械,A001,螺栓,一百,0.5,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert record.min_confidence == 0.4
    assert record.review_status is ReviewStatus.PENDING
    assert any(issue.code is IssueCode.TYPE_MISMATCH for issue in record.issues)


def test_blank_optional_field_is_not_a_type_mismatch(settings, purchase_order, write_csv):
    """空的数值/日期单元格不是「类型转换失败」，不该伪造 TYPE_MISMATCH 或拉低置信度."""
    path = write_csv(
        "blank_amount.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期,交付日期\n"
        "PO-2024-001,华信机械,A001,螺栓,100,0.5,,2024-03-01,2024-03-15\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert not any(issue.code is IssueCode.TYPE_MISMATCH for issue in record.issues)
    assert record.get("total_amount") is None
    assert record.min_confidence == 1.0
    assert record.review_status is ReviewStatus.AUTO_APPROVED


def test_missing_required_column_is_reported_and_blocks(settings, purchase_order, write_csv):
    path = write_csv(
        "missing_material.csv",
        "订单编号,供应商,物料名称,数量,单价,下单日期\nPO-2024-001,华信机械,螺栓,100,0.5,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    table_issues = result.sources[0].table_issues
    assert any(issue.code is IssueCode.MISSING_FIELD and issue.field == "material_id" for issue in table_issues)
    assert result.records[0].review_status is ReviewStatus.PENDING


def test_cross_field_error_routes_to_review(settings, purchase_order, write_csv):
    path = write_csv(
        "bad_date.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期,交付日期\n"
        "PO-2024-001,华信机械,A001,螺栓,10,1,2024-03-10,2024-03-01\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    record = result.records[0]
    assert any(issue.code is IssueCode.CROSS_FIELD and issue.level.value == "error" for issue in record.issues)
    assert record.review_status is ReviewStatus.PENDING


def test_duplicate_primary_key_is_table_level_issue(settings, purchase_order, write_csv):
    path = write_csv(
        "dup_key.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期\n"
        "PO-2024-001,华信机械,A001,螺栓,10,1,2024-03-01\n"
        "PO-2024-001,远大五金,A002,支架,5,2,2024-03-02\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    assert any(issue.code is IssueCode.DUPLICATE_KEY for issue in result.sources[0].table_issues)


def test_source_status_reflects_record_level_issues(settings, purchase_order, write_csv):
    """来源状态不能只看表级问题：表级干净但记录级有错误的来源，状态必须是 error 而非 ok."""
    path = write_csv(
        "record_level_only.csv",
        "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期,交付日期,是否加急,备注\n"
        "PO-2024-301,华信机械,A001,螺栓,5,0.5,2.5,2024-03-06,2024-03-01,否,交付早于下单\n"
        "PO-2024-302,远大五金,B002,齿轮,20,12.5,250,2024-03-02,2024-03-20,否,正常行\n",
    )
    result = run_pipeline(purchase_order, settings, [path])

    source = result.sources[0]
    assert source.table_issues == []  # 表级确实干净
    assert source.record_issue_levels["error"] >= 1
    assert source.status == "error"  # 修复前这里会误报成 ok
    assert result.review_pending == 1


def test_broken_file_is_collected_as_failure_without_aborting_batch(settings, purchase_order, order_csv, tmp_path):
    missing = tmp_path / "does_not_exist.csv"
    result = run_pipeline(purchase_order, settings, [order_csv, missing])

    assert result.total_records == 2
    assert [failure.file_name for failure in result.failures] == ["does_not_exist.csv"]


def test_ingest_unsupported_suffix(tmp_path, settings, purchase_order):
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    result = run_pipeline(purchase_order, settings, [path])
    assert result.total_records == 0
    assert len(result.failures) == 1
