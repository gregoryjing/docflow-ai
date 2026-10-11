"""数据去重：精确（主键相同）+ 模糊（多字段相似度）.

设计硬约束：**模糊去重的结果必须进人工复核，绝不自动合并**。
数据合并错误的代价远高于多花几分钟人工确认，这是企业场景的底线。
因此本模块只「发现 + 标注」，不删改任何记录。
"""

from __future__ import annotations

from rapidfuzz import fuzz

from docflow.config import Settings
from docflow.models.dedupe import DedupeResult, DuplicateGroup, DuplicateMode
from docflow.models.record import IssueCode, IssueLevel, QualityIssue, ReviewStatus, UnifiedRecord
from docflow.models.schema import SchemaDefinition
from docflow.text import is_blank, normalize_key


class Deduplicator:
    """按 Schema 的 dedupe 配置扫描重复实体."""

    def __init__(self, schema: SchemaDefinition, settings: Settings) -> None:
        self.schema = schema
        self.settings = settings
        self.config = schema.dedupe
        self._labels = {definition.key: definition.label for definition in schema.fields}

    def _label(self, key: str) -> str:
        return self._labels.get(key, key)

    def run(self, records: list[UnifiedRecord]) -> DedupeResult:
        if not self.config.enabled:
            return DedupeResult()

        exact = self._exact_groups(records)
        claimed = {record_id for group in exact for record_id in group.record_ids}
        fuzzy = self._fuzzy_groups(records, claimed)
        self._flag_fuzzy(records, fuzzy)
        return DedupeResult(groups=[*exact, *fuzzy])

    # ── 精确：主键归一化后相同 ────────────────────────────────
    def _exact_groups(self, records: list[UnifiedRecord]) -> list[DuplicateGroup]:
        key = self.schema.primary_key
        if key is None:
            return []

        buckets: dict[str, list[UnifiedRecord]] = {}
        for record in records:
            value = record.get(key)
            if is_blank(value):
                continue
            buckets.setdefault(normalize_key(str(value)), []).append(record)

        groups: list[DuplicateGroup] = []
        for group in buckets.values():
            if len(group) < 2:
                continue
            display = str(group[0].get(key))
            groups.append(
                DuplicateGroup(
                    key_value=display,
                    mode=DuplicateMode.EXACT,
                    record_ids=[record.record_id for record in group],
                    similarity=1.0,
                    review_required=False,
                    detail=f"主键「{self._label(key)}」相同，共 {len(group)} 条记录指向同一实体",
                )
            )
        return groups

    # ── 模糊：主键缺失时用多字段相似度兜底 ────────────────────
    def _fuzzy_groups(self, records: list[UnifiedRecord], claimed: set[str]) -> list[DuplicateGroup]:
        if not self.config.fields:
            return []

        key = self.schema.primary_key
        candidates = [
            record for record in records if record.record_id not in claimed and not self._has_reliable_key(record, key)
        ]

        groups: list[DuplicateGroup] = []
        used: set[str] = set()
        for index, anchor in enumerate(candidates):
            if anchor.record_id in used:
                continue
            cluster = [anchor]
            weakest = 1.0
            for other in candidates[index + 1 :]:
                if other.record_id in used:
                    continue
                score = self._similarity(anchor, other)
                if score >= self.config.threshold:
                    cluster.append(other)
                    weakest = min(weakest, score)
            if len(cluster) < 2:
                continue
            used.update(record.record_id for record in cluster)
            groups.append(
                DuplicateGroup(
                    key_value=self._identity_label(cluster[0]),
                    mode=DuplicateMode.FUZZY,
                    record_ids=[record.record_id for record in cluster],
                    similarity=round(weakest, 4),
                    review_required=True,
                    detail=(
                        f"主键「{self._label(key or '')}」缺失，"
                        f"「{'、'.join(self._label(field) for field in self.config.fields)}」"
                        f"相似度 {weakest:.0%} ≥ {self.config.threshold:.0%}，疑似同一实体"
                    ),
                )
            )
        return groups

    def _has_reliable_key(self, record: UnifiedRecord, key: str | None) -> bool:
        """主键有值就认为标识可靠，不做模糊匹配（避免把不同实体误判为重复）."""
        return key is not None and not is_blank(record.get(key))

    def _similarity(self, left: UnifiedRecord, right: UnifiedRecord) -> float:
        """各字段归一化后 token_sort_ratio 的均值；全空则相似度为 0."""
        scores: list[float] = []
        for field_key in self.config.fields:
            a, b = left.get(field_key), right.get(field_key)
            if is_blank(a) or is_blank(b):
                continue
            scores.append(fuzz.token_sort_ratio(normalize_key(str(a)), normalize_key(str(b))) / 100.0)
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _identity_label(self, record: UnifiedRecord) -> str:
        parts = [str(record.get(field)) for field in self.config.fields if not is_blank(record.get(field))]
        return " / ".join(parts) if parts else "（无标识）"

    def _flag_fuzzy(self, records: list[UnifiedRecord], fuzzy_groups: list[DuplicateGroup]) -> None:
        """给疑似重复记录打标并强制进复核队列."""
        index = {record.record_id: record for record in records}
        for group in fuzzy_groups:
            for record_id in group.record_ids:
                record = index.get(record_id)
                if record is None:  # pragma: no cover - record_id 来自同一列表
                    continue
                record.issues.append(
                    QualityIssue(
                        level=IssueLevel.WARNING,
                        code=IssueCode.POSSIBLE_DUPLICATE,
                        message=group.detail,
                        confidence=group.similarity,
                    )
                )
                record.review_status = ReviewStatus.PENDING
