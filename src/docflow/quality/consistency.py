"""跨来源一致性校验：四层质检体系的最外层.

字段级 / 记录级挂在单条记录上，表级挂在单张表上，而跨源级必须「同时看到整次运行的全部记录」
才能判断——同一个物料编号在 A、B 两个供应商的报价单里，名称/规格是否一致。

只报「不同来源之间」的冲突：同一文件内部的不一致，已经由表级的主键/整行重复规则覆盖，
再报一次只会制造噪声。
"""

from __future__ import annotations

from docflow.models.record import IssueCode, QualityIssue, UnifiedRecord
from docflow.models.schema import ConsistencyRule, SchemaDefinition
from docflow.text import is_blank


class CrossSourceChecker:
    """按 Schema 里声明的 consistency 规则，在多来源记录之间做一致性比对."""

    def __init__(self, schema: SchemaDefinition) -> None:
        self.schema = schema
        self._labels = {definition.key: definition.label for definition in schema.fields}

    def _label(self, key: str) -> str:
        return self._labels.get(key, key)

    def check(self, records: list[UnifiedRecord]) -> None:
        """就地给冲突记录追加 CROSS_SOURCE 问题，不改变记录数量与顺序."""
        if not self.schema.consistency:
            return
        for rule in self.schema.consistency:
            self._check_rule(rule, records)

    def _check_rule(self, rule: ConsistencyRule, records: list[UnifiedRecord]) -> None:
        groups: dict[str, list[UnifiedRecord]] = {}
        for record in records:
            value = record.get(rule.key)
            if is_blank(value):
                continue
            groups.setdefault(str(value), []).append(record)

        for key_value, group in groups.items():
            # 跨源规则的语义前提是「多个来源」；单来源的分组不在此处判
            if len({record.source_file for record in group}) < 2:
                continue
            for field_key in rule.fields:
                self._check_field(rule, key_value, field_key, group)

    def _check_field(
        self,
        rule: ConsistencyRule,
        key_value: str,
        field_key: str,
        group: list[UnifiedRecord],
    ) -> None:
        values: dict[str, list[UnifiedRecord]] = {}
        for record in group:
            value = record.get(field_key)
            if is_blank(value):
                continue
            values.setdefault(str(value), []).append(record)

        if len(values) < 2:  # 全部一致（或该字段全空）→ 无冲突
            return

        summary = "、".join(f"「{value}」（{len(rows)} 条）" for value, rows in values.items())
        message = rule.message or (
            f"同一「{self._label(rule.key)}」{key_value} 在不同来源中的"
            f"「{self._label(field_key)}」取值不一致：{summary}"
        )
        for record in group:
            value = record.get(field_key)
            if is_blank(value):
                continue
            record.issues.append(
                QualityIssue(
                    level=rule.level,
                    code=IssueCode.CROSS_SOURCE,
                    message=message,
                    field=field_key,
                    row_index=record.row_index,
                    value=value,
                )
            )
