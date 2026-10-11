from __future__ import annotations

import ast
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from docflow.formula import parse, referenced_names
from docflow.models.record import IssueLevel
from docflow.text import normalize_key

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class FieldType(StrEnum):
    """标准字段的数据类型，决定质检时用哪套校验规则."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATE = "date"
    BOOLEAN = "boolean"


class FieldDefinition(BaseModel):
    """一个标准字段的定义：怎么叫（label/synonyms）、什么类型、要不要必填."""

    key: str
    label: str
    type: FieldType = FieldType.STRING
    required: bool = False
    synonyms: list[str] = Field(default_factory=list, description="同义词，用于字段映射的精确匹配")
    pattern: str | None = Field(default=None, description="正则校验，如日期、编号格式")
    min_value: float | None = None
    max_value: float | None = None
    description: str = ""

    @field_validator("key")
    @classmethod
    def _key_must_be_snake(cls, value: str) -> str:
        if not _KEY_PATTERN.match(value):
            raise ValueError(f"字段 key 必须是小写字母开头的 snake_case：{value!r}")
        return value

    def all_names(self) -> list[str]:
        """label + synonyms，用于建索引."""
        return [self.label, *self.synonyms]


class CheckDefinition(BaseModel):
    """跨字段 / 条件校验规则，声明在 Schema 里，由质检引擎统一执行.

    目前支持三种：
    - compare：left op right（如 单价 >= 最低价）
    - require_if：if_field 等于 if_value 时，then_field 必填
    - formula：target ≈ expression（如 总金额 ≈ 数量 × 单价），支持 + - * /
    """

    type: Literal["compare", "require_if", "formula"]
    level: IssueLevel = IssueLevel.WARNING
    message: str = ""

    left: str | None = None
    op: Literal["<", "<=", ">", ">=", "==", "!="] | None = None
    right: str | None = None

    if_field: str | None = None
    if_value: str | None = None
    then_field: str | None = None

    target: str | None = Field(default=None, description="formula：待校验字段 key（如总金额）")
    expression: str | None = Field(default=None, description="formula：算术表达式，引用其他字段 key")
    tolerance: float | None = Field(
        default=None, ge=0.0, description="formula：允许的相对误差（0.01 表示 1%），默认 0.01"
    )

    _formula_tree: ast.Expression | None = PrivateAttr(default=None)
    _formula_names: set[str] = PrivateAttr(default_factory=set)

    @model_validator(mode="after")
    def _validate_shape(self) -> CheckDefinition:
        if self.type == "compare":
            if not (self.left and self.op and self.right):
                raise ValueError("compare 规则必须同时提供 left / op / right")
        elif self.type == "require_if":
            if not (self.if_field and self.then_field):
                raise ValueError("require_if 规则必须同时提供 if_field / then_field")
        else:
            if not (self.target and self.expression):
                raise ValueError("formula 规则必须同时提供 target / expression")
            # 解析失败会抛 FormulaError（ValueError 子类），在 Schema 加载期即暴露
            self._formula_tree = parse(self.expression)
            self._formula_names = referenced_names(self._formula_tree)
        return self

    @property
    def formula_names(self) -> set[str]:
        return set(self._formula_names)

    @property
    def formula_tree(self) -> ast.Expression:
        if self._formula_tree is None:  # pragma: no cover - 仅 formula 类型会走到
            raise ValueError("非 formula 规则没有表达式")
        return self._formula_tree


class ConsistencyRule(BaseModel):
    """跨来源一致性规则：同一实体（key 字段）在不同来源里，fields 的取值应当一致.

    例：同一个物料编号在 A 供应商和 B 供应商的报价单里，名称/规格必须相同。
    这类冲突只有在「同时看到多个来源的记录」时才能发现，属于四层质检体系的最外层。
    """

    key: str = Field(description="实体标识字段 key，如物料编号")
    fields: list[str] = Field(description="同一实体下应保持一致的字段 key 列表")
    level: IssueLevel = IssueLevel.WARNING
    message: str = ""

    @field_validator("fields")
    @classmethod
    def _fields_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("consistency 规则至少要指定一个待比对字段")
        return value


class DedupeConfig(BaseModel):
    """去重配置：精确去重看主键，模糊去重在主键缺失时用多字段相似度兜底."""

    enabled: bool = Field(default=False, description="是否启用去重扫描")
    fields: list[str] = Field(default_factory=list, description="模糊去重参与比对的字段 key")
    threshold: float = Field(default=0.9, ge=0.0, le=1.0, description="模糊相似度达到此值判定为疑似同一实体")


class SchemaDefinition(BaseModel):
    """一套标准字段的集合，等价于「这张表应该长什么样」的契约."""

    name: str
    label: str
    description: str = ""
    primary_key: str | None = Field(default=None, description="主键字段 key，用于精确去重")
    fields: list[FieldDefinition]
    checks: list[CheckDefinition] = Field(default_factory=list, description="跨字段校验规则")
    consistency: list[ConsistencyRule] = Field(default_factory=list, description="跨来源一致性规则")
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig, description="去重配置")

    # ── 查询 ──────────────────────────────────────────────────
    def field(self, key: str) -> FieldDefinition | None:
        return next((f for f in self.fields if f.key == key), None)

    def field_keys(self) -> list[str]:
        return [f.key for f in self.fields]

    def required_keys(self) -> list[str]:
        return [f.key for f in self.fields if f.required]

    def column_hint(self) -> list[str]:
        """给 OCR / VL 模型的列名提示：label 在前、synonyms 在后，去重保序.

        VL 提示词里带上这些名字，模型更可能把表头写成下游能精确匹配的写法。
        """
        names: list[str] = []
        for definition in self.fields:
            for name in definition.all_names():
                if name and name not in names:
                    names.append(name)
        return names

    def synonym_index(self) -> dict[str, str]:
        """归一化名称 → 字段 key。同义词冲突时先定义者优先，保证结果稳定."""
        index: dict[str, str] = {}
        for definition in self.fields:
            for name in definition.all_names():
                index.setdefault(normalize_key(name), definition.key)
        return index

    @model_validator(mode="after")
    def _check_rule_references(self) -> SchemaDefinition:
        """一致性 / 去重 / 算术规则引用的字段必须真实存在，否则规则会静默失效."""
        known = {definition.key for definition in self.fields}
        for rule in self.consistency:
            missing = [key for key in (rule.key, *rule.fields) if key not in known]
            if missing:
                raise ValueError(f"consistency 规则引用了未定义的字段：{', '.join(missing)}")
        unknown = [key for key in self.dedupe.fields if key not in known]
        if unknown:
            raise ValueError(f"dedupe.fields 引用了未定义的字段：{', '.join(unknown)}")
        for check in self.checks:
            if check.type != "formula":
                continue
            refs = {check.target} | check.formula_names if check.target else check.formula_names
            bad = sorted(key for key in refs if key is not None and key not in known)
            if bad:
                raise ValueError(f"formula 规则引用了未定义的字段：{', '.join(bad)}")
        return self

    # ── 加载 ──────────────────────────────────────────────────
    @classmethod
    def from_toml(cls, path: Path) -> SchemaDefinition:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        raw_fields = raw.pop("fields", {})
        payload = {**raw, "fields": [{"key": key, **value} for key, value in raw_fields.items()]}
        return cls.model_validate(payload)


def load_schema(path: Path) -> SchemaDefinition:
    if not path.exists():
        raise FileNotFoundError(f"Schema 文件不存在：{path}")
    return SchemaDefinition.from_toml(path)


def load_schemas(directory: Path) -> dict[str, SchemaDefinition]:
    """加载目录下所有 *.toml Schema，返回 name → 定义。文件损坏时直接抛错，不静默跳过."""
    if not directory.exists():
        return {}
    schemas: dict[str, SchemaDefinition] = {}
    for path in sorted(directory.glob("*.toml")):
        schema = load_schema(path)
        schemas[schema.name] = schema
    return schemas
