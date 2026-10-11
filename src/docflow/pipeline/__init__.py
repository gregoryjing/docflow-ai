"""编排层：把接入 / 映射 / 质检串成一条可复用、可配置、可断点续跑的处理管线."""

from docflow.pipeline.checkpoint import Checkpoint, CheckpointStore, compute_signature
from docflow.pipeline.report import FileFailure, PipelineResult, SourceOutcome
from docflow.pipeline.runner import ProcessingPipeline, run_pipeline
from docflow.pipeline.spec import (
    CANONICAL_ORDER,
    DEFAULT_SPEC_NAME,
    REQUIRES,
    PipelineSpec,
    StepSpec,
    builtin_default,
    load_spec,
    load_specs,
    resolve_spec,
)
from docflow.pipeline.state import PipelineState, TableContext

__all__ = [
    "CANONICAL_ORDER",
    "DEFAULT_SPEC_NAME",
    "REQUIRES",
    "Checkpoint",
    "CheckpointStore",
    "FileFailure",
    "PipelineResult",
    "PipelineSpec",
    "PipelineState",
    "ProcessingPipeline",
    "SourceOutcome",
    "StepSpec",
    "TableContext",
    "builtin_default",
    "compute_signature",
    "load_spec",
    "load_specs",
    "resolve_spec",
    "run_pipeline",
]
