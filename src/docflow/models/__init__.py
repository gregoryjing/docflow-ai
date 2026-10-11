"""统一数据模型：Schema 定义、原始表、标准化记录、字段映射结果.

这一层是整个平台的数据契约——接入层产出 RawTable，映射层产出 MappingResult，
质检层产出带 QualityIssue 的 UnifiedRecord，导出层只认 UnifiedRecord。
"""

from docflow.models.mapping import ColumnMapping, MappingMethod, MappingResult
from docflow.models.record import (
    FieldValue,
    IssueCode,
    IssueLevel,
    QualityIssue,
    ReviewStatus,
    UnifiedRecord,
)
from docflow.models.schema import (
    CheckDefinition,
    FieldDefinition,
    FieldType,
    SchemaDefinition,
    load_schema,
    load_schemas,
)
from docflow.models.table import RawTable, SourceKind, SourceRef

__all__ = [
    "CheckDefinition",
    "ColumnMapping",
    "FieldDefinition",
    "FieldType",
    "FieldValue",
    "IssueCode",
    "IssueLevel",
    "MappingMethod",
    "MappingResult",
    "QualityIssue",
    "RawTable",
    "ReviewStatus",
    "SchemaDefinition",
    "SourceKind",
    "SourceRef",
    "UnifiedRecord",
    "load_schema",
    "load_schemas",
]
