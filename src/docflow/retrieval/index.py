"""知识检索索引：把已处理记录切成片段，用纯 Python BM25 做统一检索.

设计取舍：
- 不引入向量库 / 分词库：BM25 确定性、离线可跑、可解释，适合「按编号 / 术语找记录」；
- 中文用字符二元组切词（见 `tokenize.py`），英文数字按词；
- 索引按 run 落盘为 JSON，`search_all` 可跨多个已建索引的运行统一检索——
  这正是「结构化数据 + 非结构化文档」统一检索的落点：两者最终都摊平成文本片段。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from docflow.errors import StorageError
from docflow.retrieval.models import Chunk, SearchHit
from docflow.retrieval.tokenize import tokenize
from docflow.store.models import RecordRow

# BM25 参数：k1 控制词频饱和，b 控制长度归一化强度，取信息检索的常用默认值
_K1 = 1.5
_B = 0.75


def chunk_from_row(row: RecordRow, max_chars: int = 600) -> Chunk:
    """把一条记录摊平成检索片段：保留标准字段值，来源坐标随片段走."""
    lines = [f"{key}: {value}" for key, value in (row.payload or {}).items() if value not in (None, "")]
    text = "\n".join(lines)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
    return Chunk(
        chunk_id=f"{row.run_id}:{row.record_id}",
        run_id=row.run_id,
        schema_name=row.schema_name,
        source_file=row.source_file,
        source_location=row.source_location,
        record_id=row.record_id,
        text=text,
    )


class RetrievalIndex(BaseModel):
    """一次运行的检索索引：片段 + 词频统计，可直接序列化为 JSON."""

    run_id: str
    chunks: list[Chunk] = Field(default_factory=list)
    term_freqs: dict[str, dict[str, int]] = Field(default_factory=dict)
    doc_lengths: dict[str, int] = Field(default_factory=dict)
    doc_freq: dict[str, int] = Field(default_factory=dict)
    avg_length: float = 0.0

    @classmethod
    def build(cls, run_id: str, rows: Sequence[RecordRow], max_chars: int = 600) -> RetrievalIndex:
        chunks = [chunk for row in rows if (chunk := chunk_from_row(row, max_chars)).text.strip()]
        term_freqs: dict[str, dict[str, int]] = {}
        doc_lengths: dict[str, int] = {}
        doc_freq: Counter[str] = Counter()
        for chunk in chunks:
            counts = Counter(tokenize(chunk.text))
            term_freqs[chunk.chunk_id] = dict(counts)
            doc_lengths[chunk.chunk_id] = sum(counts.values())
            for term in counts:
                doc_freq[term] += 1
        avg_length = sum(doc_lengths.values()) / len(doc_lengths) if doc_lengths else 0.0
        return cls(
            run_id=run_id,
            chunks=chunks,
            term_freqs=term_freqs,
            doc_lengths=doc_lengths,
            doc_freq=dict(doc_freq),
            avg_length=avg_length,
        )

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        query_terms = set(tokenize(query))
        if not query_terms or not self.chunks:
            return []

        total = len(self.chunks)
        hits: list[SearchHit] = []
        for chunk in self.chunks:
            freqs = self.term_freqs.get(chunk.chunk_id, {})
            length = self.doc_lengths.get(chunk.chunk_id, 0)
            score = 0.0
            matched: list[str] = []
            for term in query_terms:
                freq = freqs.get(term, 0)
                if freq == 0:
                    continue
                df = self.doc_freq.get(term, 0)
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                norm = 1 - _B + _B * (length / self.avg_length if self.avg_length else 0.0)
                score += idf * (freq * (_K1 + 1)) / (freq + _K1 * norm)
                matched.append(term)
            if score > 0:
                hits.append(SearchHit(chunk=chunk, score=round(score, 6), matched_terms=sorted(matched)))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]


class RetrievalStore:
    """索引的落盘与读取：每个 run 一个 JSON，目录即索引清单."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, run_id: str) -> Path:
        return self.directory / f"{run_id}.json"

    def save(self, index: RetrievalIndex) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(index.run_id)
        target.write_text(index.model_dump_json(indent=2), encoding="utf-8")
        return target

    def load(self, run_id: str) -> RetrievalIndex | None:
        target = self.path(run_id)
        if not target.exists():
            return None
        try:
            return RetrievalIndex.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError(f"读取检索索引失败：{run_id}（{exc}）") from exc

    def run_ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(path.stem for path in self.directory.glob("*.json"))

    def search_all(self, query: str, top_k: int = 5, run_id: str | None = None) -> list[SearchHit]:
        """跨索引检索：指定 run 只搜该次运行，否则合并所有已建索引后统一排序."""
        identifiers = [run_id] if run_id else self.run_ids()
        hits: list[SearchHit] = []
        for identifier in identifiers:
            index = self.load(identifier)
            if index is not None:
                hits.extend(index.search(query, top_k=top_k))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]
