"""步骤级断点续跑：每完成一步就把状态落盘，中断后从最近一步继续.

与「文件级指纹账本」（`batch.ProcessedLedger`）的区别：
- 账本回答「这个文件处理过没有」，粒度是整个文件、跨多次运行；
- 检查点回答「这条管线跑到第几步了」，粒度是步骤、服务于单次运行的续跑。

签名把 Schema、规范名、步骤序列、输入文件（名 + 大小）绑在一起：任何一项变了，
旧检查点自动失效，避免「拿 A 规范的半成品去接着跑 B 规范」这类静默错误。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from docflow.errors import PipelineError
from docflow.pipeline.state import PipelineState


class Checkpoint(BaseModel):
    """一次落盘：完成到第几步 + 当时的状态快照."""

    signature: str
    schema_name: str
    spec_name: str
    steps: list[str] = Field(default_factory=list)
    completed_index: int = 0
    completed_step: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    state: PipelineState = Field(default_factory=PipelineState)


def _file_digest(path: Path) -> str:
    """文件内容的 SHA-256 摘要（分块读取，避免大文件一次性载入内存）."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_signature(schema_name: str, spec_name: str, steps: list[str], paths: list[Path]) -> str:
    """把「这次跑的是什么」压成一个短哈希，作为检查点的身份.

    绑定文件**内容**而非仅「名字 + 大小」：同名同大小但内容不同的文件（例如覆盖保存、
    或两份恰好字节数相同的文件）必须让旧检查点失效，否则会拿旧状态的半成品接着跑，
    静默产出与当前输入不符的结果。
    """
    files: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        try:
            files.append(f"{path.name}:{_file_digest(path)}")
        except OSError:  # 读不到内容时退化为「名字 + 大小」，至少不阻断签名计算
            files.append(f"{path.name}:{path.stat().st_size}")
    payload = {
        "schema": schema_name,
        "spec": spec_name,
        "steps": list(steps),
        "files": sorted(files),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class CheckpointStore:
    """检查点仓库：一个签名一个文件，目录不存在时按需创建."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, signature: str) -> Path:
        return self.directory / f"{signature}.json"

    def load(self, signature: str) -> Checkpoint | None:
        path = self._path(signature)
        if not path.exists():
            return None
        try:
            return Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PipelineError(f"检查点损坏：{path.name}（{exc}）") from exc

    def save(self, checkpoint: Checkpoint) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(checkpoint.signature).write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")

    def clear(self, signature: str) -> None:
        self._path(signature).unlink(missing_ok=True)
