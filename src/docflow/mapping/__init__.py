"""映射层：源表列名 → Schema 标准字段 → 统一记录."""

from docflow.mapping.apply import apply_mapping, make_record_id
from docflow.mapping.matcher import ColumnMatcher

__all__ = ["ColumnMatcher", "apply_mapping", "make_record_id"]
