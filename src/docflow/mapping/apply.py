"""把映射结论落到数据上：RawTable + MappingResult → 统一记录列表."""

from __future__ import annotations

import hashlib

from docflow.coerce import coerce_value
from docflow.models.mapping import MappingResult
from docflow.models.record import FieldValue, IssueCode, IssueLevel, QualityIssue, UnifiedRecord
from docflow.models.schema import SchemaDefinition
from docflow.models.table import RawTable
from docflow.text import is_blank

# 类型转换失败时压到的置信度上限——这类值必须被人工看到
_TYPE_MISMATCH_CONFIDENCE = 0.4


def make_record_id(schema_name: str, table: RawTable, row_index: int, primary_value: object) -> str:
    """确定性 ID：同一份文件重复处理得到同样的 ID，便于幂等与去重."""
    seed = f"{schema_name}|{table.source.file_name}|{table.source.location}|{row_index}|{primary_value or ''}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def apply_mapping(
    table: RawTable,
    mapping: MappingResult,
    schema: SchemaDefinition,
) -> list[UnifiedRecord]:
    """按列映射把每一行转成 UnifiedRecord.

    只有**映射成功**的列会进入 fields——未映射字段不在记录里留空占位，
    这样 min_confidence 反映的才是「真实取到值的那些字段有多可信」。
    """
    active = [column for column in mapping.columns if column.is_mapped and column.target_field]

    records: list[UnifiedRecord] = []
    for row_index, row in enumerate(table.rows):
        fields: dict[str, FieldValue] = {}
        issues: list[QualityIssue] = []

        for column in active:
            key = column.target_field
            if key is None:  # pragma: no cover - active 已过滤
                continue
            definition = schema.field(key)
            if definition is None:  # pragma: no cover - 映射由同一 Schema 生成
                continue

            raw = row.get(column.source_column)
            # 空值不是「类型转换失败」：空单元格该不该报错由质检层按 required 判定，
            # 在这里强行 coerce 会为每个空的数值/日期列伪造一条 TYPE_MISMATCH，
            # 把置信度压到 0.4 并让整条记录进复核队列。
            if is_blank(raw):
                fields[key] = FieldValue(
                    value=None,
                    raw=raw,
                    confidence=column.confidence,
                    source_column=column.source_column,
                )
                continue

            value, error = coerce_value(raw, definition.type)
            confidence = column.confidence
            if error is not None:
                confidence = min(confidence, _TYPE_MISMATCH_CONFIDENCE)
                issues.append(
                    QualityIssue(
                        level=IssueLevel.WARNING,
                        code=IssueCode.TYPE_MISMATCH,
                        message=error,
                        field=key,
                        row_index=row_index,
                        value=raw,
                        confidence=confidence,
                    )
                )

            fields[key] = FieldValue(
                value=value,
                raw=raw,
                confidence=confidence,
                source_column=column.source_column,
            )

        primary_value = fields[schema.primary_key].value if schema.primary_key in fields else None
        records.append(
            UnifiedRecord(
                record_id=make_record_id(schema.name, table, row_index, primary_value),
                schema_name=schema.name,
                source_file=table.source.file_name,
                source_location=table.source.location,
                row_index=row_index,
                fields=fields,
                issues=issues,
            )
        )
    return records
