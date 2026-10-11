"""字段映射：三级策略 + 冲突消解."""

from __future__ import annotations

from docflow.mapping.matcher import ColumnMatcher
from docflow.models.mapping import MappingMethod
from docflow.models.table import RawTable, SourceKind, SourceRef


def _table(columns: list[str]) -> RawTable:
    return RawTable(
        source=SourceRef(file_name="t.csv", kind=SourceKind.CSV),
        columns=columns,
        rows=[{column: "" for column in columns}],
    )


def test_exact_field_name_scores_full_confidence(settings, purchase_order):
    mapping = ColumnMatcher(purchase_order, settings).match_column("order_no")
    assert mapping.target_field == "order_no"
    assert mapping.method is MappingMethod.EXACT
    assert mapping.confidence == 1.0


def test_synonym_dictionary_is_hit(settings, purchase_order):
    mapping = ColumnMatcher(purchase_order, settings).match_column("采购单号")
    assert mapping.target_field == "order_no"
    assert mapping.method is MappingMethod.SYNONYM
    assert mapping.confidence == 1.0


def test_fuzzy_match_for_unlisted_variant(settings, purchase_order):
    mapping = ColumnMatcher(purchase_order, settings).match_column("supplier name")
    assert mapping.target_field == "supplier"
    assert mapping.method is MappingMethod.FUZZY
    assert mapping.confidence >= 0.75


def test_unknown_column_stays_unmapped(settings, purchase_order):
    mapping = ColumnMatcher(purchase_order, settings).match_column("zzzz")
    assert not mapping.is_mapped
    assert mapping.method is MappingMethod.NONE


def test_two_columns_competing_for_same_field_keep_the_stronger(settings, purchase_order):
    result = ColumnMatcher(purchase_order, settings).map_table(_table(["订单号", "订单编号", "供应商"]))

    winners = [column for column in result.columns if column.target_field == "order_no"]
    assert len(winners) == 1
    assert winners[0].source_column == "订单号"

    demoted = next(column for column in result.columns if column.source_column == "订单编号")
    assert not demoted.is_mapped
    assert demoted.method is MappingMethod.NONE
    assert "竞争" in demoted.reason


def test_missing_required_field_is_surfaced(settings, purchase_order):
    result = ColumnMatcher(purchase_order, settings).map_table(
        _table(["订单编号", "供应商", "物料名称", "数量", "单价", "下单日期"])
    )
    assert "material_id" in result.missing_required
