"""检索层数据模型：片段、命中、回答.

片段（Chunk）是检索的最小单位：一条结构化记录会被摊平成一段「字段: 值」的文本，
并保留来源（文件 / 位置 / 记录 ID），保证任何一次检索结果都能倒推回原文。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """可被检索的一段文本及其来源坐标."""

    chunk_id: str
    run_id: str
    schema_name: str = ""
    source_file: str = ""
    source_location: str = ""
    record_id: str = ""
    text: str = ""

    def citation(self) -> str:
        parts = [self.source_file or "(未知来源)"]
        if self.source_location:
            parts.append(self.source_location)
        return " · ".join(parts)


class SearchHit(BaseModel):
    """一次检索命中：片段 + 得分 + 命中的词项."""

    chunk: Chunk
    score: float
    matched_terms: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    """RAG 回答：答案正文 + 引用片段；generated 表示是否由模型生成."""

    question: str
    answer: str
    citations: list[SearchHit] = Field(default_factory=list)
    generated: bool = False
