"""知识检索（RAG）：切词、BM25 索引、跨运行检索、抽取式问答与 Web 页面."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.retrieval import RagAnswerer, RetrievalIndex, RetrievalStore, chunk_from_row, tokenize
from docflow.store.models import RecordRow

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _row(run_id: str, record_id: str, **payload: object) -> RecordRow:
    return RecordRow(
        run_id=run_id,
        record_id=record_id,
        schema_name="purchase_order",
        source_file="orders.csv",
        source_location="第2行",
        payload=payload,
    )


# ── 切词 ──────────────────────────────────────────────────────
def test_tokenize_handles_cjk_and_ascii() -> None:
    tokens = tokenize("物料编号 PO-2024-001")
    assert {"po", "2024", "001"} <= set(tokens)
    assert {"物料", "料编", "编号"} <= set(tokens)


def test_tokenize_single_cjk_char() -> None:
    assert "钢" in tokenize("钢")


# ── 索引与检索 ────────────────────────────────────────────────
def test_chunk_from_row_truncates_text() -> None:
    chunk = chunk_from_row(_row("run1", "a", 物料名称="螺栓", 备注="x" * 500), max_chars=50)
    assert len(chunk.text) == 50


def test_index_search_ranks_relevant_chunk() -> None:
    rows = [
        _row("run1", "a", 物料名称="螺栓", 供应商="华信机械"),
        _row("run1", "b", 物料名称="齿轮", 供应商="远大五金"),
    ]
    hits = RetrievalIndex.build("run1", rows).search("螺栓", top_k=5)

    assert hits
    assert hits[0].chunk.record_id == "a"
    assert hits[0].score > 0
    assert "螺栓" in hits[0].matched_terms


def test_index_skips_empty_records() -> None:
    rows = [_row("run1", "a"), _row("run1", "b", 物料名称="螺栓")]
    index = RetrievalIndex.build("run1", rows)
    assert [chunk.record_id for chunk in index.chunks] == ["b"]


def test_search_empty_query_returns_nothing() -> None:
    index = RetrievalIndex.build("run1", [_row("run1", "a", 物料名称="螺栓")])
    assert index.search("   ") == []


# ── 落盘与跨运行检索 ──────────────────────────────────────────
def test_store_roundtrip_and_cross_run_search(tmp_path: Path) -> None:
    store = RetrievalStore(tmp_path / "retrieval")
    store.save(RetrievalIndex.build("run1", [_row("run1", "a", 物料名称="螺栓")]))
    store.save(RetrievalIndex.build("run2", [_row("run2", "b", 物料名称="齿轮")]))

    assert store.run_ids() == ["run1", "run2"]
    assert [hit.chunk.run_id for hit in store.search_all("螺栓")] == ["run1"]
    assert store.search_all("螺栓", run_id="run2") == []

    loaded = store.load("run1")
    assert loaded is not None
    assert len(loaded.chunks) == 1
    assert store.load("missing") is None


# ── 问答 ──────────────────────────────────────────────────────
def _hits() -> list:
    index = RetrievalIndex.build("run1", [_row("run1", "a", 物料名称="螺栓", 数量=100)])
    return index.search("螺栓")


def test_answer_extractive_when_llm_missing(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", schemas_dir=SCHEMAS_DIR)
    answer = RagAnswerer(settings).answer("螺栓是什么", _hits())

    assert answer.generated is False
    assert "螺栓" in answer.answer
    assert answer.citations


def test_answer_generated_when_llm_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        schemas_dir=SCHEMAS_DIR,
        llm_base_url="http://127.0.0.1:9",
        llm_model="qwen2.5:7b",
    )
    monkeypatch.setattr(RagAnswerer, "_post", lambda self, question, hits: "螺栓是紧固件 [1]")
    answer = RagAnswerer(settings).answer("螺栓是什么", _hits())

    assert answer.generated is True
    assert "[1]" in answer.answer


def test_answer_without_hits(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", schemas_dir=SCHEMAS_DIR)
    answer = RagAnswerer(settings).answer("无关问题", [])
    assert answer.generated is False
    assert answer.citations == []


# ── Web 页面 ──────────────────────────────────────────────────
def test_web_search_page_finds_indexed_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DOCFLOW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DOCFLOW_SCHEMAS_DIR", str(SCHEMAS_DIR))

    RetrievalStore(data_dir / "state" / "retrieval").save(
        RetrievalIndex.build("run1", [_row("run1", "a", 物料名称="螺栓")])
    )

    from docflow.web.app import app

    with TestClient(app) as client:
        response = client.get("/search", params={"q": "螺栓"})

    assert response.status_code == 200
    assert "螺栓" in response.text
    assert "run1" in response.text
