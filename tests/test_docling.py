"""Docling 可插拔高级解析后端：缺失依赖时安全报错，可用时把表格/正文转成 RawTable.

测试不真正安装 Docling（依赖重），而是用「假转换器」替换 `_build_converter`，
从而验证 `parse_with_docling` 的解析、清洗与兜底逻辑，以及接入层的路由分发。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from docflow.config import Settings
from docflow.errors import ParseError
from docflow.ingestion import docling_backend, ingest_file
from docflow.ingestion.docling_backend import parse_with_docling


class _FakeFrame:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.columns = list(records[0]) if records else []

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self._records


class _FakeTable:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def export_to_dataframe(self, doc: Any = None) -> _FakeFrame:
        return _FakeFrame(self._records)


class _FakeDocument:
    def __init__(self, tables: list[Any] | None = None, markdown: str = "") -> None:
        self.tables = tables or []
        self._markdown = markdown

    def export_to_markdown(self) -> str:
        return self._markdown


class _FakeConverter:
    def __init__(self, document: _FakeDocument) -> None:
        self._document = document

    def convert(self, path: str) -> Any:
        return type("_Result", (), {"document": self._document})()


def _pdf(tmp_path: Path, name: str = "sample.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% fake\n")
    return path


def _force_docling(monkeypatch: pytest.MonkeyPatch, document: _FakeDocument) -> None:
    monkeypatch.setattr(docling_backend, "docling_available", lambda: True)
    monkeypatch.setattr(docling_backend, "_build_converter", lambda settings: _FakeConverter(document))


# ── 依赖缺失时的行为 ──────────────────────────────────────────
def test_parse_without_dependency_raises_install_hint(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(docling_backend, "docling_available", lambda: False)
    with pytest.raises(ParseError, match="docling"):
        parse_with_docling(_pdf(tmp_path), settings)


# ── 有表格：正常抽取并清洗 ────────────────────────────────────
def test_parse_extracts_and_cleans_tables(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    document = _FakeDocument(
        tables=[
            _FakeTable(
                [
                    {"物料名称": "螺栓", "数量": 100},
                    {"物料名称": "齿轮", "数量": float("nan")},
                    {"物料名称": None, "数量": None},
                ]
            )
        ]
    )
    _force_docling(monkeypatch, document)

    tables = parse_with_docling(_pdf(tmp_path), settings)

    assert len(tables) == 1
    assert tables[0].row_count == 2  # 全空行被丢弃，NaN 被清成空值
    assert tables[0].notes == ["由 Docling 后端解析"]
    assert tables[0].source.file_name == "sample.pdf"


# ── 无表格：退回「键：值」抽取 ────────────────────────────────
def test_parse_falls_back_to_key_value(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    document = _FakeDocument(markdown="供应商: 华信机械\n数量: 100\n单价: 0.5")
    _force_docling(monkeypatch, document)

    tables = parse_with_docling(_pdf(tmp_path), settings)

    assert len(tables) == 1
    # 键值抽取会被转置成「一个键一列、一行数据」，才能进入后续字段映射
    assert tables[0].row_count == 1
    assert set(tables[0].columns) == {"供应商", "数量", "单价"}
    assert any("键：值" in note for note in tables[0].notes)


def test_parse_returns_empty_when_nothing_usable(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    _force_docling(monkeypatch, _FakeDocument(markdown="只是一段没有结构的正文"))

    assert parse_with_docling(_pdf(tmp_path), settings) == []


# ── 接入层路由 ────────────────────────────────────────────────
def test_ingest_routes_pdf_to_docling_when_configured(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def _spy(path: Path, s: Settings, column_hint: Any = None) -> list[Any]:
        calls.append(path.name)
        return []

    monkeypatch.setattr(docling_backend, "parse_with_docling", _spy)
    docling_settings = settings.model_copy(update={"document_backend": "docling"})

    ingest_file(_pdf(tmp_path), docling_settings)

    assert calls == ["sample.pdf"]


def test_ingest_keeps_builtin_backend_by_default(settings: Settings, monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("默认后端不应走 Docling")

    def _spy_pdf(path: Path, s: Settings, column_hint: Any = None) -> list[Any]:
        calls.append(path.name)
        return []

    monkeypatch.setattr(docling_backend, "parse_with_docling", _boom)
    monkeypatch.setattr("docflow.ingestion.parse_pdf", _spy_pdf)

    assert ingest_file(_pdf(tmp_path), settings) == []
    assert calls == ["sample.pdf"]
