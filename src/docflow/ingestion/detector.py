"""文件类型检测：先看扩展名，再用文件头做一次确认，避免「改名文件」被误判."""

from __future__ import annotations

from pathlib import Path

from docflow.errors import ParseError, UnsupportedFormatError
from docflow.models.table import SourceKind

_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_CSV_SUFFIXES = {".csv", ".tsv"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"  # xlsx / docx 本质都是 zip
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # 旧版 xls / doc


def read_magic(path: Path, size: int = 8) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError as exc:
        raise ParseError(f"文件不可读：{path.name}（{exc}）") from exc


def detect_kind(path: Path) -> SourceKind:
    """按扩展名给出解析分支，并用文件头做一次廉价确认."""
    suffix = path.suffix.lower()
    magic = read_magic(path)

    if suffix in _EXCEL_SUFFIXES:
        if not magic.startswith((_ZIP_MAGIC, _OLE_MAGIC)):
            raise ParseError(f"{path.name} 扩展名是 Excel，但文件头不是表格格式，可能已损坏或改过名")
        return SourceKind.EXCEL

    if suffix == ".xls":
        raise UnsupportedFormatError(f"{path.name}：暂不支持旧版 .xls，请用 Excel 另存为 .xlsx 后重试")

    if suffix in _CSV_SUFFIXES:
        return SourceKind.CSV

    if suffix == ".pdf":
        if not magic.startswith(_PDF_MAGIC):
            raise ParseError(f"{path.name} 扩展名是 PDF，但文件头不是 %PDF，可能已损坏或改过名")
        # 文本型 / 扫描型在解析阶段按文本层字符数判定
        return SourceKind.PDF_TEXT

    if suffix in _IMAGE_SUFFIXES:
        return SourceKind.IMAGE

    if suffix == ".docx":
        if not magic.startswith(_ZIP_MAGIC):
            raise ParseError(f"{path.name} 扩展名是 Word，但文件头不是 zip 容器，可能已损坏或改过名")
        return SourceKind.DOCX

    raise UnsupportedFormatError(
        f"不支持的文件类型：{path.name}。当前支持 Excel(.xlsx)、CSV/TSV、PDF、图片、Word(.docx)"
    )
