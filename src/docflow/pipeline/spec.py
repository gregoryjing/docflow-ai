"""管线定义：用 TOML 声明「这次处理要跑哪些步骤、按什么顺序」.

设计取舍：
- 用 TOML 而非 YAML：`tomllib` 是标准库（零新依赖），且与 `schemas/*.toml` 保持一致；
- 步骤顺序必须落在一条**规范顺序**上——否则用户把 `triage` 排在 `consistency` 之前，
  「ERROR 级跨源冲突推动记录进复核队列」这条链路会静默失效；
- 步骤间有硬依赖（`quality` 依赖 `apply` 等），缺前置步骤的 spec 直接拒绝加载。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from docflow.errors import SchemaError

# 规范顺序：任何 spec 的步骤序列都必须是它的子序列
CANONICAL_ORDER: tuple[str, ...] = (
    "ingest",
    "map",
    "apply",
    "quality",
    "consistency",
    "triage",
    "dedupe",
)

# 步骤的硬依赖：缺了前置步骤，该步骤就没有输入
REQUIRES: dict[str, tuple[str, ...]] = {
    "ingest": (),
    "map": ("ingest",),
    "apply": ("map",),
    "quality": ("apply",),
    "consistency": ("apply",),
    "triage": ("quality",),
    "dedupe": ("triage",),
}

DEFAULT_SPEC_NAME = "default"


class StepSpec(BaseModel):
    """一个步骤的声明。`enabled=false` 可保留步骤位置但跳过执行."""

    name: str
    enabled: bool = True


class PipelineSpec(BaseModel):
    """一条管线定义：步骤序列 + 是否启用断点续跑."""

    name: str
    label: str = ""
    description: str = ""
    checkpoint: bool = Field(default=True, description="该规范是否允许步骤级断点续跑")
    steps: list[StepSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_steps(self) -> PipelineSpec:
        names = [step.name for step in self.steps]
        if not names:
            raise ValueError("pipeline 至少需要一个步骤")
        if len(set(names)) != len(names):
            raise ValueError(f"pipeline 步骤重复：{names}")

        unknown = [name for name in names if name not in CANONICAL_ORDER]
        if unknown:
            raise ValueError(f"未知步骤：{', '.join(unknown)}；可用：{', '.join(CANONICAL_ORDER)}")

        positions = [CANONICAL_ORDER.index(name) for name in names]
        if positions != sorted(positions):
            raise ValueError("步骤顺序必须遵循规范顺序：" + " → ".join(CANONICAL_ORDER))

        active = {step.name for step in self.steps if step.enabled}
        for name in active:
            missing = [dep for dep in REQUIRES[name] if dep not in active]
            if missing:
                raise ValueError(f"步骤「{name}」缺少前置步骤：{', '.join(missing)}")
        return self

    def active_steps(self) -> list[str]:
        """按声明顺序返回启用中的步骤名."""
        return [step.name for step in self.steps if step.enabled]


def builtin_default() -> PipelineSpec:
    """内置兜底规范：即使 `pipelines/` 目录缺失也能跑通完整链路."""
    return PipelineSpec(
        name=DEFAULT_SPEC_NAME,
        label="标准处理链路",
        description="接入 → 映射 → 应用 → 质检 → 跨源一致性 → 分流 → 去重",
        steps=[StepSpec(name=name) for name in CANONICAL_ORDER],
    )


def load_spec(path: Path) -> PipelineSpec:
    if not path.exists():
        raise SchemaError(f"Pipeline 规范不存在：{path}")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SchemaError(f"Pipeline 规范解析失败：{path.name}（{exc}）") from exc
    try:
        return PipelineSpec.model_validate(raw)
    except ValueError as exc:
        raise SchemaError(f"Pipeline 规范校验失败：{path.name}（{exc}）") from exc


def load_specs(directory: Path) -> dict[str, PipelineSpec]:
    """加载目录下所有 *.toml 规范，返回 name → 定义."""
    if not directory.exists():
        return {}
    specs: dict[str, PipelineSpec] = {}
    for path in sorted(directory.glob("*.toml")):
        spec = load_spec(path)
        specs[spec.name] = spec
    return specs


def resolve_spec(directory: Path, name: str) -> PipelineSpec:
    """按名字取规范；`default` 永远可用（目录缺失时退回内置定义）."""
    specs = load_specs(directory)
    if name in specs:
        return specs[name]
    if name == DEFAULT_SPEC_NAME:
        return builtin_default()
    available = ", ".join(sorted(specs)) or "（目录为空）"
    raise SchemaError(f"未知 Pipeline 规范：{name}；可用：{available}")
