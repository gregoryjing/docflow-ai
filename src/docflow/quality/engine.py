"""质检引擎：字段级 → 记录级 → 表级，三层规则依次执行.

设计取舍：
- **记录级问题**（必填缺失、格式不符、跨字段冲突）挂在对应记录上，随记录一起流转；
- **表级问题**（主键重复、整列全空）返回给调用方单独展示，不污染每条记录。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from docflow.formula import evaluate
from docflow.models.record import (
    IssueCode,
    IssueLevel,
    QualityIssue,
    UnifiedRecord,
)
from docflow.models.schema import CheckDefinition, FieldDefinition, SchemaDefinition
from docflow.text import is_blank

_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _comparable(left: object, right: object) -> tuple[object, object]:
    """把两个值拉到可比较的同一域：都是数字就按数字比，否则按字符串比（ISO 日期字符串可直接比）."""
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return left, right
    return str(left), str(right)


class QualityEngine:
    """按 Schema 定义的规则检查记录，并把问题写回记录 / 返回表级问题."""

    def __init__(self, schema: SchemaDefinition) -> None:
        self.schema = schema
        self._patterns: dict[str, re.Pattern[str]] = {
            definition.key: re.compile(definition.pattern) for definition in schema.fields if definition.pattern
        }
        self._labels = {definition.key: definition.label for definition in schema.fields}

    def label(self, key: str | None) -> str:
        if key is None:
            return ""
        return self._labels.get(key, key)

    # ── 对外入口 ──────────────────────────────────────────────
    def check(self, records: list[UnifiedRecord]) -> list[QualityIssue]:
        """检查一批记录。记录级问题直接追加到 record.issues，返回表级问题列表."""
        for record in records:
            record.issues.extend(self._check_record(record))
        return self._check_table(records)

    # ── 字段级 + 记录级 ───────────────────────────────────────
    def _check_record(self, record: UnifiedRecord) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for definition in self.schema.fields:
            field = record.fields.get(definition.key)
            value = field.value if field is not None else None

            if definition.required and is_blank(value):
                issues.append(
                    QualityIssue(
                        level=IssueLevel.ERROR,
                        code=IssueCode.MISSING_REQUIRED,
                        message=f"必填字段「{definition.label}」为空",
                        field=definition.key,
                        row_index=record.row_index,
                        value=value,
                    )
                )
                continue

            if is_blank(value):
                continue

            issues.extend(self._check_value(definition, value, record.row_index))

        issues.extend(self._check_cross_fields(record))
        return issues

    def _check_value(self, definition: FieldDefinition, value: object, row_index: int | None) -> list[QualityIssue]:
        issues: list[QualityIssue] = []

        pattern = self._patterns.get(definition.key)
        if pattern is not None and not pattern.search(str(value)):
            issues.append(
                QualityIssue(
                    level=IssueLevel.WARNING,
                    code=IssueCode.PATTERN_MISMATCH,
                    message=f"「{definition.label}」不符合格式要求（{definition.pattern}）：{value}",
                    field=definition.key,
                    row_index=row_index,
                    value=value,
                )
            )

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if definition.min_value is not None and value < definition.min_value:
                issues.append(
                    QualityIssue(
                        level=IssueLevel.WARNING,
                        code=IssueCode.RANGE_VIOLATION,
                        message=f"「{definition.label}」小于允许的最小值 {definition.min_value}：{value}",
                        field=definition.key,
                        row_index=row_index,
                        value=value,
                    )
                )
            if definition.max_value is not None and value > definition.max_value:
                issues.append(
                    QualityIssue(
                        level=IssueLevel.WARNING,
                        code=IssueCode.RANGE_VIOLATION,
                        message=f"「{definition.label}」大于允许的最大值 {definition.max_value}：{value}",
                        field=definition.key,
                        row_index=row_index,
                        value=value,
                    )
                )
        return issues

    def _check_cross_fields(self, record: UnifiedRecord) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for check in self.schema.checks:
            if check.type == "compare":
                issue = self._run_compare(check, record)
            elif check.type == "require_if":
                issue = self._run_require_if(check, record)
            else:
                issue = self._run_formula(check, record)
            if issue is not None:
                issues.append(issue)
        return issues

    def _run_compare(self, check: CheckDefinition, record: UnifiedRecord) -> QualityIssue | None:
        left, right = record.get(check.left or ""), record.get(check.right or "")
        if is_blank(left) or is_blank(right):
            return None  # 缺值交给必填校验处理，这里不重复报
        comparator = _COMPARATORS[check.op or "=="]
        try:
            a, b = _comparable(left, right)
            satisfied = comparator(a, b)
        except TypeError:  # pragma: no cover - _comparable 已归一化类型
            return None
        if satisfied:
            return None
        default = (
            f"「{self.label(check.left)}」{check.op}「{self.label(check.right)}」不成立（{left} {check.op} {right}）"
        )
        return QualityIssue(
            level=check.level,
            code=IssueCode.CROSS_FIELD,
            message=check.message or default,
            field=check.left,
            row_index=record.row_index,
            value=left,
        )

    def _run_require_if(self, check: CheckDefinition, record: UnifiedRecord) -> QualityIssue | None:
        condition = record.get(check.if_field or "")
        if str(condition).strip() != (check.if_value or "").strip():
            return None
        target = record.get(check.then_field or "")
        if not is_blank(target):
            return None
        default = (
            f"当「{self.label(check.if_field)}」为「{check.if_value}」时，「{self.label(check.then_field)}」不能为空"
        )
        return QualityIssue(
            level=check.level,
            code=IssueCode.CROSS_FIELD,
            message=check.message or default,
            field=check.then_field,
            row_index=record.row_index,
        )

    def _run_formula(self, check: CheckDefinition, record: UnifiedRecord) -> QualityIssue | None:
        """校验 target ≈ expression（如 总金额 ≈ 数量 × 单价）.

        参与计算的任一字段缺值或非数值时直接跳过：这类问题由必填 / 类型校验负责，
        在这里再报一次只会制造重复噪音，还可能把「格式问题」误报成「算错账」。
        """
        target = record.get(check.target or "")
        if is_blank(target) or isinstance(target, bool) or not isinstance(target, (int, float)):
            return None
        values: dict[str, float] = {}
        for name in check.formula_names:
            value = record.get(name)
            if is_blank(value) or isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            values[name] = float(value)
        expected = evaluate(check.formula_tree, values)
        tolerance = check.tolerance if check.tolerance is not None else 0.01
        allowed = max(0.01, abs(expected) * tolerance)
        actual = float(target)
        if abs(actual - expected) <= allowed:
            return None
        default = f"「{self.label(check.target)}」与「{check.expression}」不一致（实际 {actual:g}，应为 {expected:g}）"
        return QualityIssue(
            level=check.level,
            code=IssueCode.CROSS_FIELD,
            message=check.message or default,
            field=check.target,
            row_index=record.row_index,
            value=actual,
        )

    # ── 表级 ──────────────────────────────────────────────────
    def _check_table(self, records: list[UnifiedRecord]) -> list[QualityIssue]:
        if not records:
            return []
        issues: list[QualityIssue] = []
        issues.extend(self._check_primary_key(records))
        issues.extend(self._check_duplicate_rows(records))
        issues.extend(self._check_empty_columns(records))
        return issues

    def _check_primary_key(self, records: list[UnifiedRecord]) -> list[QualityIssue]:
        key = self.schema.primary_key
        if key is None:
            return []
        groups: dict[str, list[UnifiedRecord]] = {}
        for record in records:
            value = record.get(key)
            if is_blank(value):
                continue
            groups.setdefault(str(value), []).append(record)

        issues: list[QualityIssue] = []
        for value, group in groups.items():
            if len(group) < 2:
                continue
            rows = ", ".join(str(record.row_index + 1) for record in group if record.row_index is not None)
            issues.append(
                QualityIssue(
                    level=IssueLevel.WARNING,
                    code=IssueCode.DUPLICATE_KEY,
                    message=f"主键「{self.label(key)}」重复出现 {len(group)} 次：{value}"
                    + (f"（第 {rows} 行）" if rows else ""),
                    field=key,
                    value=value,
                )
            )
        return issues

    def _check_duplicate_rows(self, records: list[UnifiedRecord]) -> list[QualityIssue]:
        groups: dict[tuple[tuple[str, str], ...], list[UnifiedRecord]] = {}
        for record in records:
            signature = tuple(sorted((name, str(field.value)) for name, field in record.fields.items()))
            if not signature:
                continue
            groups.setdefault(signature, []).append(record)

        issues: list[QualityIssue] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            for record in group:
                issues.append(
                    QualityIssue(
                        level=IssueLevel.INFO,
                        code=IssueCode.DUPLICATE_ROW,
                        message=f"该行与另外 {len(group) - 1} 行所有字段完全一致",
                        row_index=record.row_index,
                    )
                )
        return issues

    def _check_empty_columns(self, records: list[UnifiedRecord]) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for definition in self.schema.fields:
            present = [r for r in records if definition.key in r.fields]
            if not present:
                continue
            if any(not is_blank(r.get(definition.key)) for r in present):
                continue
            issues.append(
                QualityIssue(
                    level=IssueLevel.WARNING,
                    code=IssueCode.EMPTY_COLUMN,
                    message=(
                        f"整列「{definition.label}」在 {len(present)} 条记录中均为空"
                        + ("（该字段为必填）" if definition.required else "")
                    ),
                    field=definition.key,
                    confidence=1.0,
                )
            )
        return issues
