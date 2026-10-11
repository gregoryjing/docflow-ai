"""处理管线：按 Pipeline 规范逐步执行「接入 → 映射 → 应用 → 质检 → 一致性 → 分流 → 去重」.

三条设计约束：
- 步骤顺序由 `PipelineSpec` 声明并校验，管线只按 `active_steps()` 的顺序执行；
- 步骤之间只通过 `PipelineState` 传递数据，状态显式可序列化；
- 传入检查点目录时，每完成一步就把状态落盘，中断后从最近一步续跑。

管线本身仍是**纯内存 + 无副作用**的：只吃路径、吐 `PipelineResult`，
落库与导出由调用方决定。这样 CLI、Web、测试可以复用同一套逻辑。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from docflow.config import Settings
from docflow.ingestion import ingest_paths
from docflow.mapping.advisor import FieldMappingAdvisor, LlmFieldMappingAdvisor
from docflow.mapping.apply import apply_mapping
from docflow.mapping.matcher import ColumnMatcher
from docflow.models.mapping import MappingResult
from docflow.models.record import IssueCode, IssueLevel, QualityIssue, ReviewStatus, UnifiedRecord
from docflow.models.schema import SchemaDefinition
from docflow.pipeline.checkpoint import Checkpoint, CheckpointStore, compute_signature
from docflow.pipeline.report import FileFailure, FileWarning, PipelineResult, SourceOutcome
from docflow.pipeline.spec import PipelineSpec, builtin_default
from docflow.pipeline.state import PipelineState, TableContext
from docflow.quality.consistency import CrossSourceChecker
from docflow.quality.dedupe import Deduplicator
from docflow.quality.engine import QualityEngine
from docflow.quality.status import assign_review_status


class ProcessingPipeline:
    """按一份 Schema + 一条 Pipeline 规范处理一批文件."""

    def __init__(
        self,
        schema: SchemaDefinition,
        settings: Settings,
        advisor: FieldMappingAdvisor | None = None,
        spec: PipelineSpec | None = None,
    ) -> None:
        self.schema = schema
        self.settings = settings
        self.spec = spec or builtin_default()
        # 未显式注入时，按配置决定是否启用 L3；未配置则 advisor 为空，行为与两级映射一致
        if advisor is None and settings.llm_configured:
            advisor = LlmFieldMappingAdvisor(settings)
        self.matcher = ColumnMatcher(schema, settings, advisor)
        self.quality = QualityEngine(schema)
        self.cross_source = CrossSourceChecker(schema)
        self.deduplicator = Deduplicator(schema, settings)
        self._steps: dict[str, Callable[[PipelineState], None]] = {
            "ingest": self._step_ingest,
            "map": self._step_map,
            "apply": self._step_apply,
            "quality": self._step_quality,
            "consistency": self._step_consistency,
            "triage": self._step_triage,
            "dedupe": self._step_dedupe,
        }

    # ── 主流程 ────────────────────────────────────────────────
    def run(self, paths: list[Path], checkpoint_dir: Path | None = None) -> PipelineResult:
        started = datetime.now(UTC)
        steps = self.spec.active_steps()
        store = CheckpointStore(checkpoint_dir) if checkpoint_dir is not None and self.spec.checkpoint else None
        signature = compute_signature(self.schema.name, self.spec.name, steps, paths) if store else ""

        state = PipelineState(paths=list(paths))
        start_index = 0
        resumed_from: str | None = None
        if store is not None:
            saved = store.load(signature)
            if saved is not None:
                state = saved.state
                start_index = saved.completed_index + 1
                started = saved.started_at
                resumed_from = saved.completed_step

        for index in range(start_index, len(steps)):
            name = steps[index]
            self._steps[name](state)
            if store is not None:
                store.save(
                    Checkpoint(
                        signature=signature,
                        schema_name=self.schema.name,
                        spec_name=self.spec.name,
                        steps=steps,
                        completed_index=index,
                        completed_step=name,
                        started_at=started,
                        state=state,
                    )
                )

        if store is not None:  # 全部跑完，删除检查点：下次再跑就是一次全新运行
            store.clear(signature)

        return self._assemble(state, started, steps, start_index, resumed_from)

    # ── 各步骤 ────────────────────────────────────────────────
    def _step_ingest(self, state: PipelineState) -> None:
        tables, failures, warnings = ingest_paths(state.paths, self.settings, column_hint=self.schema.column_hint())
        state.tables = tables
        state.failures = [FileFailure(file_name=name, error=error) for name, error in failures]
        state.warnings = [FileWarning(file_name=name, message=message) for name, message in warnings]

    def _step_map(self, state: PipelineState) -> None:
        state.contexts = [TableContext(table=table, mapping=self.matcher.map_table(table)) for table in state.tables]

    def _step_apply(self, state: PipelineState) -> None:
        for context in state.contexts:
            if context.mapping is None:  # pragma: no cover - spec 校验保证 map 在 apply 之前
                raise RuntimeError("apply 步骤缺少映射结果")
            context.records = apply_mapping(context.table, context.mapping, self.schema)

    def _step_quality(self, state: PipelineState) -> None:
        for context in state.contexts:
            mapping = context.mapping or MappingResult(source=context.table.source, schema_name=self.schema.name)
            context.table_issues = [*self._mapping_issues(mapping), *self.quality.check(context.records)]

    def _step_consistency(self, state: PipelineState) -> None:
        self.cross_source.check(state.records)

    def _step_triage(self, state: PipelineState) -> None:
        assign_review_status(state.records, self.settings)

    def _step_dedupe(self, state: PipelineState) -> None:
        state.dedupe = self.deduplicator.run(state.records)

    # ── 组装结果 ──────────────────────────────────────────────
    def _assemble(
        self,
        state: PipelineState,
        started: datetime,
        steps: list[str],
        start_index: int,
        resumed_from: str | None,
    ) -> PipelineResult:
        sources = [
            SourceOutcome(
                source=context.table.source,
                mapping=context.mapping or MappingResult(source=context.table.source, schema_name=self.schema.name),
                record_count=len(context.records),
                review_pending=sum(1 for record in context.records if record.review_status is ReviewStatus.PENDING),
                table_issues=context.table_issues,
                record_issue_levels=_level_counts(context.records),
            )
            for context in state.contexts
        ]

        return PipelineResult(
            run_id=uuid.uuid4().hex[:12],
            schema_name=self.schema.name,
            schema_label=self.schema.label,
            started_at=started,
            finished_at=datetime.now(UTC),
            records=state.records,
            sources=sources,
            failures=state.failures,
            file_warnings=state.warnings,
            dedupe=state.dedupe,
            pipeline_name=self.spec.name,
            executed_steps=steps[start_index:],
            resumed_from=resumed_from,
        )

    def _mapping_issues(self, mapping: MappingResult) -> list[QualityIssue]:
        """把映射阶段的问题提升为表级问题，让用户一眼看到「哪些数据没进来」."""
        issues: list[QualityIssue] = []

        for column in mapping.unmapped_columns():
            issues.append(
                QualityIssue(
                    level=IssueLevel.INFO,
                    code=IssueCode.UNMAPPED_COLUMN,
                    message=f"源列「{column.source_column}」未映射到标准字段，该列数据未纳入",
                    confidence=column.confidence,
                )
            )

        for key in mapping.missing_required:
            definition = self.schema.field(key)
            label = definition.label if definition else key
            issues.append(
                QualityIssue(
                    level=IssueLevel.ERROR,
                    code=IssueCode.MISSING_FIELD,
                    message=f"必填字段「{label}」在源文件中找不到对应列，该字段全部记录为空",
                    field=key,
                )
            )
        return issues


def _level_counts(records: list[UnifiedRecord]) -> dict[str, int]:
    """把一批记录的问题按等级汇总，供来源状态判断使用."""
    counts = {level.value: 0 for level in IssueLevel}
    for record in records:
        for issue in record.issues:
            counts[issue.level.value] += 1
    return counts


def run_pipeline(
    schema: SchemaDefinition,
    settings: Settings,
    paths: list[Path],
    advisor: FieldMappingAdvisor | None = None,
    spec: PipelineSpec | None = None,
    checkpoint_dir: Path | None = None,
) -> PipelineResult:
    """便捷函数，等价于 `ProcessingPipeline(schema, settings, advisor, spec).run(paths, checkpoint_dir)`."""
    return ProcessingPipeline(schema, settings, advisor, spec).run(paths, checkpoint_dir=checkpoint_dir)
