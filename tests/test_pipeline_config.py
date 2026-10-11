"""Pipeline 规范校验 + 步骤级断点续跑的测试（纯内存 / 临时目录，无外部依赖）."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from docflow.errors import SchemaError
from docflow.pipeline import (
    CANONICAL_ORDER,
    Checkpoint,
    CheckpointStore,
    PipelineSpec,
    ProcessingPipeline,
    StepSpec,
    compute_signature,
    load_specs,
    resolve_spec,
    run_pipeline,
)
from docflow.pipeline.spec import builtin_default

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINES_DIR = REPO_ROOT / "pipelines"

DUP_KEY_CSV = (
    "订单编号,供应商,物料编号,物料名称,数量,单价,下单日期\n"
    "PO-2024-001,华信机械,A001,螺栓,10,1,2024-03-01\n"
    "PO-2024-001,远大五金,A002,支架,5,2,2024-03-02\n"
)


# ── 规范校验 ──────────────────────────────────────────────────
def test_builtin_default_covers_canonical_order() -> None:
    spec = builtin_default()
    assert spec.active_steps() == list(CANONICAL_ORDER)
    assert spec.checkpoint is True


def test_spec_rejects_unknown_step() -> None:
    with pytest.raises(ValidationError):
        PipelineSpec(name="bad", steps=[StepSpec(name="ingest"), StepSpec(name="explode")])


def test_spec_rejects_wrong_order() -> None:
    with pytest.raises(ValidationError):
        PipelineSpec(name="bad", steps=[StepSpec(name="triage"), StepSpec(name="quality")])


def test_spec_rejects_missing_dependency() -> None:
    with pytest.raises(ValidationError):
        PipelineSpec(name="bad", steps=[StepSpec(name="map")])


def test_spec_rejects_duplicate_steps() -> None:
    with pytest.raises(ValidationError):
        PipelineSpec(name="bad", steps=[StepSpec(name="ingest"), StepSpec(name="ingest")])


def test_disabled_step_excluded_from_active() -> None:
    steps = [StepSpec(name=name, enabled=name != "dedupe") for name in CANONICAL_ORDER]
    spec = PipelineSpec(name="no_dedupe", steps=steps)
    assert "dedupe" not in spec.active_steps()
    assert spec.active_steps() == list(CANONICAL_ORDER[:-1])


def test_load_specs_from_repo() -> None:
    specs = load_specs(PIPELINES_DIR)
    assert {"standard", "fast"} <= set(specs)
    assert specs["fast"].active_steps() == ["ingest", "map", "apply", "quality", "triage"]


def test_resolve_default_falls_back_to_builtin(tmp_path: Path) -> None:
    spec = resolve_spec(tmp_path / "missing", "default")
    assert spec.active_steps() == list(CANONICAL_ORDER)


def test_resolve_unknown_spec_raises() -> None:
    with pytest.raises(SchemaError):
        resolve_spec(PIPELINES_DIR, "does_not_exist")


# ── 规范驱动执行 ──────────────────────────────────────────────
def test_fast_spec_skips_consistency_and_dedupe(settings, purchase_order, write_csv) -> None:
    path = write_csv("dup.csv", DUP_KEY_CSV)
    spec = resolve_spec(PIPELINES_DIR, "fast")
    result = run_pipeline(purchase_order, settings, [path], spec=spec)

    assert result.pipeline_name == "fast"
    assert result.executed_steps == ["ingest", "map", "apply", "quality", "triage"]
    assert result.duplicate_groups == 0  # 去重步骤未执行


def test_disabled_step_is_not_executed(settings, purchase_order, write_csv) -> None:
    path = write_csv("dup.csv", DUP_KEY_CSV)
    steps = [StepSpec(name=name, enabled=name != "dedupe") for name in CANONICAL_ORDER]
    spec = PipelineSpec(name="no_dedupe", label="跳过去重", steps=steps)
    result = run_pipeline(purchase_order, settings, [path], spec=spec)

    assert "dedupe" not in result.executed_steps
    assert result.duplicate_groups == 0


# ── 检查点 ────────────────────────────────────────────────────
def test_signature_changes_with_inputs(tmp_path: Path) -> None:
    first = tmp_path / "a.csv"
    first.write_text("x", encoding="utf-8")
    second = tmp_path / "b.csv"
    second.write_text("x", encoding="utf-8")
    sig_first = compute_signature("s", "default", ["ingest"], [first])
    sig_second = compute_signature("s", "default", ["ingest"], [second])
    assert sig_first != sig_second


def test_checkpoint_store_roundtrip(tmp_path: Path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = Checkpoint(
        signature="abc123",
        schema_name="purchase_order",
        spec_name="default",
        steps=list(CANONICAL_ORDER),
        completed_index=0,
        completed_step="ingest",
    )
    store.save(checkpoint)
    loaded = store.load("abc123")
    assert loaded is not None
    assert loaded.completed_step == "ingest"

    store.clear("abc123")
    assert store.load("abc123") is None


def test_checkpoint_cleared_after_successful_run(settings, purchase_order, order_csv, tmp_path: Path) -> None:
    checkpoint_dir = tmp_path / "ckpt"
    result = run_pipeline(purchase_order, settings, [order_csv], checkpoint_dir=checkpoint_dir)

    assert result.resumed_from is None
    assert list(checkpoint_dir.glob("*.json")) == []


def test_checkpoint_resume_after_interruption(settings, purchase_order, order_csv, tmp_path: Path, monkeypatch) -> None:
    checkpoint_dir = tmp_path / "ckpt"
    spec = builtin_default()

    def boom(self: ProcessingPipeline, state: object) -> None:
        raise RuntimeError("模拟处理中断")

    monkeypatch.setattr(ProcessingPipeline, "_step_dedupe", boom)
    first = ProcessingPipeline(purchase_order, settings, spec=spec)
    with pytest.raises(RuntimeError):
        first.run([order_csv], checkpoint_dir=checkpoint_dir)

    saved = list(checkpoint_dir.glob("*.json"))
    assert len(saved) == 1  # 中断前已把「triage 完成」落盘

    monkeypatch.undo()
    second = ProcessingPipeline(purchase_order, settings, spec=spec)
    result = second.run([order_csv], checkpoint_dir=checkpoint_dir)

    assert result.resumed_from == "triage"
    assert result.executed_steps == ["dedupe"]
    assert result.total_records == 2
    assert list(checkpoint_dir.glob("*.json")) == []  # 跑完即清理
