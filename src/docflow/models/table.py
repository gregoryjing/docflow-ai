from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SourceKind(StrEnum):
    """来源文件类型，决定解析链路走哪条分支."""

    EXCEL = "excel"
    CSV = "csv"
    PDF_TEXT = "pdf_text"
    PDF_OCR = "pdf_ocr"
    IMAGE = "image"
    DOCX = "docx"


class SourceRef(BaseModel):
    """数据来源定位：哪个文件的哪一部分."""

    file_name: str
    kind: SourceKind
    file_path: str = ""
    location: str = Field(default="", description="Sheet 名 / 第 N 页 / 图片名")

    def label(self) -> str:
        return f"{self.file_name}#{self.location}" if self.location else self.file_name


class RawTable(BaseModel):
    """接入层的统一输出：一张「有列名、有行数据」的原始表.

    接入层只负责把文件读成表，不做任何字段标准化——标准化是映射层的职责。
    """

    source: SourceRef
    columns: list[str]
    rows: list[dict[str, object]]
    header_row_index: int | None = Field(default=None, description="原文件中的表头行号（0 起），便于溯源")
    notes: list[str] = Field(default_factory=list, description="解析过程中的提示，如「跳过 3 行空行」")

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return len(self.columns)
