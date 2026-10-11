"""接入层修复的回归测试：键值表转置、空文件告警."""

from __future__ import annotations

from docflow.ingestion.header import build_table
from docflow.ingestion.keyvalue import extract_key_value_rows, key_value_table
from docflow.mapping.apply import apply_mapping
from docflow.mapping.matcher import ColumnMatcher
from docflow.models.table import SourceKind, SourceRef
from docflow.pipeline import run_pipeline


def test_key_value_table_transposes_and_keeps_duplicate_keys():
    table = key_value_table([["供应商", "华信机械"], ["数量", "100"], ["数量", "200"], ["单价", "0.5"]])

    # 键作表头、值作唯一数据行；重复键不静默丢弃，而是加后缀另立一列，值全部保留
    assert table == [["供应商", "数量", "数量#2", "单价"], ["华信机械", "100", "200", "0.5"]]


def test_key_value_lines_become_mappable_table(settings, purchase_order):
    pairs = extract_key_value_rows(
        [
            "订单编号: PO-2024-001",
            "供应商: 华信机械",
            "物料编号: A001",
            "物料名称: 螺栓",
            "数量: 100",
            "单价: 0.5",
            "下单日期: 2024-03-01",
        ]
    )
    source = SourceRef(file_name="kv.pdf", kind=SourceKind.PDF_TEXT, location="第1页")
    table = build_table(key_value_table(pairs), source, max_scan_rows=1, min_fill_ratio=1.0, header_index=0)
    assert table is not None

    mapping = ColumnMatcher(purchase_order, settings, None).map_table(table)
    records = apply_mapping(table, mapping, purchase_order)

    assert len(records) == 1
    # 关键回归：键值型 PDF 不再产出「字段全空」的记录
    assert records[0].get("supplier") == "华信机械"
    assert records[0].get("quantity") == 100
    assert records[0].get("order_no") == "PO-2024-001"


def test_empty_csv_surfaces_file_warning(settings, purchase_order, write_csv):
    path = write_csv("empty.csv", "")

    result = run_pipeline(purchase_order, settings, [path])

    assert result.total_records == 0
    assert [warning.file_name for warning in result.file_warnings] == ["empty.csv"]
    assert "CSV" in result.file_warnings[0].message


def test_zero_byte_file_surfaces_file_warning(settings, purchase_order, tmp_path):
    path = tmp_path / "zero.csv"
    path.write_bytes(b"")

    result = run_pipeline(purchase_order, settings, [path])

    assert result.total_records == 0
    assert len(result.file_warnings) == 1
    assert not result.failures
