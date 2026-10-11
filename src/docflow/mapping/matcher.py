"""字段匹配器：把源表的列名匹配到 Schema 的标准字段.

三级策略（成本从低到高）：
- L1 精确：归一化后命中字段名或同义词词典，置信度 1.0，零成本；
- L2 模糊：rapidfuzz 加权相似度 + 包含关系加成，覆盖拼写变体；
- L3 LLM：前两级都拿不准时才调用（默认关闭，需显式注入 advisor）。
"""

from __future__ import annotations

from rapidfuzz import fuzz

from docflow.config import Settings
from docflow.mapping.advisor import FieldMappingAdvisor
from docflow.models.mapping import ColumnMapping, MappingMethod, MappingResult
from docflow.models.schema import SchemaDefinition
from docflow.models.table import RawTable
from docflow.text import clean_cell, is_blank, normalize_key

# 包含关系加成：短名被长名完整包含时（如「料号」vs「供应商料号」）给一个较高的起点分。
# 刻意压在默认「自动通过」阈值（0.90）之下：包含关系本身是弱证据——「编号」既可能指
# 订单编号、也可能指物料编号，取 0.92 会让这种歧义列直接自动入库。0.88 仍高于接受阈值，
# 保证能映射上，但要人工确认后才放行。
_CONTAINMENT_BONUS = 0.88
_MIN_CONTAINMENT_LEN = 2
# 送给 L3 的样本行数：够模型判断语义，又不至于把 prompt 撑大
_LLM_SAMPLE_ROWS = 5


class ColumnMatcher:
    """有状态匹配器：初始化时把 Schema 编译成索引，之后每列匹配都是纯查表 + 打分."""

    def __init__(
        self,
        schema: SchemaDefinition,
        settings: Settings,
        advisor: FieldMappingAdvisor | None = None,
    ) -> None:
        self.schema = schema
        self.accept_threshold = settings.fuzzy_threshold
        self.suggest_threshold = settings.fuzzy_suggest_threshold
        self.advisor = advisor
        self._synonym_index = schema.synonym_index()
        self._exact_names = {
            normalize_key(name) for definition in schema.fields for name in (definition.key, definition.label)
        }
        self._candidates: dict[str, list[str]] = {
            definition.key: [normalize_key(name) for name in definition.all_names()] for definition in schema.fields
        }

    # ── 单列匹配 ──────────────────────────────────────────────
    def match_column(self, column: str, samples: list[str] | None = None) -> ColumnMapping:
        normalized = normalize_key(column)
        if not normalized:
            return ColumnMapping(source_column=column, method=MappingMethod.NONE, reason="列名为空，无法映射")

        exact = self._match_exact(column, normalized)
        if exact is not None:
            return exact

        fuzzy = self._match_fuzzy(column, normalized)
        if fuzzy.target_field is None and self.advisor is not None:
            return self._match_llm(column, samples or [], fuzzy)
        return fuzzy

    def _match_exact(self, column: str, normalized: str) -> ColumnMapping | None:
        target = self._synonym_index.get(normalized)
        if target is None:
            return None
        definition = self.schema.field(target)
        if definition is None:  # pragma: no cover - 索引由 schema 生成，必然一致
            return None
        is_exact = normalized in self._exact_names
        method = MappingMethod.EXACT if is_exact else MappingMethod.SYNONYM
        how = "与字段名一致" if is_exact else "命中同义词词典"
        return ColumnMapping(
            source_column=column,
            target_field=target,
            confidence=1.0,
            method=method,
            reason=f"「{column}」{how} → {definition.label}",
        )

    def _match_fuzzy(self, column: str, normalized: str) -> ColumnMapping:
        scored: list[tuple[str, float]] = []
        for key, names in self._candidates.items():
            best = max(fuzz.WRatio(normalized, name) for name in names) / 100.0
            if self._is_contained(normalized, names):
                best = max(best, _CONTAINMENT_BONUS)
            scored.append((key, round(best, 4)))
        scored.sort(key=lambda item: item[1], reverse=True)

        if not scored:  # pragma: no cover - Schema 无字段时上游已拦截
            return ColumnMapping(source_column=column, method=MappingMethod.NONE, reason="Schema 未定义任何字段")

        top_key, top_score = scored[0]
        alternatives = [(key, score) for key, score in scored[1:4] if score >= self.suggest_threshold]
        definition = self.schema.field(top_key)
        label = definition.label if definition else top_key

        if top_score >= self.accept_threshold:
            return ColumnMapping(
                source_column=column,
                target_field=top_key,
                confidence=top_score,
                method=MappingMethod.FUZZY,
                reason=f"「{column}」模糊匹配 → {label}（相似度 {top_score:.0%}）",
                alternatives=alternatives,
            )

        if top_score >= self.suggest_threshold:
            return ColumnMapping(
                source_column=column,
                target_field=top_key,
                confidence=top_score,
                method=MappingMethod.FUZZY,
                reason=f"「{column}」疑似 {label}（相似度 {top_score:.0%}），建议人工确认",
                alternatives=alternatives,
            )

        return ColumnMapping(
            source_column=column,
            confidence=top_score,
            method=MappingMethod.NONE,
            reason=f"未匹配到标准字段（最高相似度仅 {top_score:.0%}）",
            alternatives=alternatives,
        )

    @staticmethod
    def _is_contained(normalized: str, names: list[str]) -> bool:
        for name in names:
            if len(name) < _MIN_CONTAINMENT_LEN or len(normalized) < _MIN_CONTAINMENT_LEN:
                continue
            if name in normalized or normalized in name:
                return True
        return False

    # ── L3：LLM 兜底 ──────────────────────────────────────────
    def _match_llm(self, column: str, samples: list[str], fallback: ColumnMapping) -> ColumnMapping:
        """L1/L2 都没匹配上时，交给 advisor 判断；拿不准就保留原来的「未映射」."""
        if self.advisor is None:  # pragma: no cover - 调用前已判空
            return fallback
        advice = self.advisor.advise(column, samples, self.schema.fields)
        if advice is None or advice.target_field is None:
            return fallback
        definition = self.schema.field(advice.target_field)
        if definition is None:  # pragma: no cover - advisor 已校验字段存在
            return fallback
        return ColumnMapping(
            source_column=column,
            target_field=advice.target_field,
            confidence=advice.confidence,
            method=MappingMethod.LLM,
            reason=advice.reason or f"LLM 推断「{column}」→ {definition.label}（置信度 {advice.confidence:.0%}）",
            alternatives=fallback.alternatives,
        )

    @staticmethod
    def _samples(table: RawTable, column: str) -> list[str]:
        """取该列前若干非空值作为 L3 的语义线索."""
        samples: list[str] = []
        for row in table.rows:
            value = row.get(column)
            if is_blank(value):
                continue
            samples.append(clean_cell(value))
            if len(samples) >= _LLM_SAMPLE_ROWS:
                break
        return samples

    # ── 整表匹配 ──────────────────────────────────────────────
    def map_table(self, table: RawTable) -> MappingResult:
        mappings = self._resolve_collisions(
            [self.match_column(column, self._samples(table, column)) for column in table.columns]
        )
        mapped_keys = {m.target_field for m in mappings if m.target_field is not None}
        missing_required = [key for key in self.schema.required_keys() if key not in mapped_keys]
        return MappingResult(
            source=table.source,
            schema_name=self.schema.name,
            columns=mappings,
            missing_required=missing_required,
        )

    @staticmethod
    def _resolve_collisions(mappings: list[ColumnMapping]) -> list[ColumnMapping]:
        """多列抢同一字段时只保留置信度最高的那个，其余降级为未映射并说明原因.

        静默丢弃会让用户以为数据都映射上了；显式降级 + 原因才能让人看懂发生了什么。
        """
        winners: dict[str, ColumnMapping] = {}
        for mapping in mappings:
            if mapping.target_field is None:
                continue
            current = winners.get(mapping.target_field)
            if current is None or mapping.confidence > current.confidence:
                winners[mapping.target_field] = mapping

        resolved: list[ColumnMapping] = []
        for mapping in mappings:
            if mapping.target_field is None or winners.get(mapping.target_field) is mapping:
                resolved.append(mapping)
                continue
            winner = winners[mapping.target_field]
            resolved.append(
                mapping.model_copy(
                    update={
                        "target_field": None,
                        "method": MappingMethod.NONE,
                        "confidence": 0.0,
                        "reason": (
                            f"与列「{winner.source_column}」竞争同一字段，"
                            f"保留置信度更高的「{winner.source_column}」（{winner.confidence:.0%}）"
                        ),
                    }
                )
            )
        return resolved
