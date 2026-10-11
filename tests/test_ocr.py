"""OCR 分层链路：置信度路由 + 表格重建 + VL 回复解析（全部用假引擎，不依赖真实模型）."""

from __future__ import annotations

import pytest

from docflow.errors import OcrUnavailableError
from docflow.ocr.engine import TieredOcrEngine
from docflow.ocr.models import OcrEngineName, OcrLine, OcrPage, lines_to_matrix
from docflow.ocr.vl import _fallback_keyvalue, _user_prompt, parse_markdown_table


class _FakeEngine:
    """替身引擎：记录被调用次数，返回预置结果."""

    def __init__(self, page: OcrPage | None = None, available: bool = True) -> None:
        self.page = page
        self._available = available
        self.calls = 0
        self.last_hint: object = None

    def available(self) -> bool:
        return self._available

    def run(self, image: object, column_hint: object = None) -> OcrPage:
        self.calls += 1
        self.last_hint = column_hint
        assert self.page is not None, "假引擎未配置返回结果"
        return self.page


def _rapid_page(confidence: float) -> OcrPage:
    return OcrPage(
        lines=[OcrLine(text="编号", confidence=confidence, box=(0, 0, 40, 20))],
        engine=OcrEngineName.RAPID,
    )


def _vl_page() -> OcrPage:
    return OcrPage(matrix=[["编号", "名称"], ["A001", "螺栓"]], engine=OcrEngineName.VL)


def _engine_with(settings, rapid: _FakeEngine, vl: _FakeEngine) -> TieredOcrEngine:
    engine = TieredOcrEngine(settings)
    engine.rapid = rapid  # type: ignore[assignment]
    engine.vl = vl  # type: ignore[assignment]
    return engine


def test_ocr_disabled_raises(settings):
    engine = TieredOcrEngine(settings)  # ocr_enabled 默认 False
    with pytest.raises(OcrUnavailableError):
        engine.run(object())


def test_no_engine_available_raises(ocr_settings):
    engine = _engine_with(ocr_settings, _FakeEngine(available=False), _FakeEngine(available=False))
    with pytest.raises(OcrUnavailableError):
        engine.run(object())


def test_high_confidence_never_calls_vl(ocr_settings):
    rapid, vl = _FakeEngine(_rapid_page(0.99)), _FakeEngine(_vl_page())
    page = _engine_with(ocr_settings, rapid, vl).run(object())

    assert page.engine is OcrEngineName.RAPID
    assert rapid.calls == 1
    assert vl.calls == 0


def test_low_confidence_escalates_to_vl(ocr_settings):
    rapid, vl = _FakeEngine(_rapid_page(0.40)), _FakeEngine(_vl_page())
    page = _engine_with(ocr_settings, rapid, vl).run(object())

    assert page.engine is OcrEngineName.VL
    assert page.escalated_from is OcrEngineName.RAPID
    assert vl.calls == 1
    assert any("升级" in note for note in page.notes)


def test_column_hint_reaches_vl_engine(ocr_settings):
    rapid, vl = _FakeEngine(_rapid_page(0.40)), _FakeEngine(_vl_page())
    _engine_with(ocr_settings, rapid, vl).run(object(), column_hint=["订单编号", "供应商"])
    assert vl.last_hint == ["订单编号", "供应商"]


def test_low_confidence_without_vl_keeps_rapid_result(ocr_settings):
    rapid = _FakeEngine(_rapid_page(0.40))
    vl = _FakeEngine(available=False)
    page = _engine_with(ocr_settings, rapid, vl).run(object())

    assert page.engine is OcrEngineName.RAPID
    assert vl.calls == 0
    assert any("未配置" in note for note in page.notes)


def test_vl_only_when_rapid_missing(ocr_settings):
    rapid = _FakeEngine(available=False)
    vl = _FakeEngine(_vl_page())
    page = _engine_with(ocr_settings, rapid, vl).run(object())

    assert page.engine is OcrEngineName.VL
    assert rapid.calls == 0
    assert vl.calls == 1
    assert any("未安装一级 OCR" in note for note in page.notes)


def test_lines_to_matrix_rebuilds_columns_by_position():
    lines = [
        OcrLine(text="编号", confidence=1.0, box=(0, 0, 40, 20)),
        OcrLine(text="名称", confidence=1.0, box=(100, 0, 140, 20)),
        OcrLine(text="A001", confidence=1.0, box=(0, 30, 40, 50)),
        OcrLine(text="螺栓", confidence=1.0, box=(100, 30, 140, 50)),
    ]
    assert lines_to_matrix(lines) == [["编号", "名称"], ["A001", "螺栓"]]


def test_ocr_page_prefers_engine_supplied_matrix():
    page = OcrPage(matrix=[["a", "b"], ["", ""], ["c", "d"]], engine=OcrEngineName.VL)
    assert page.to_matrix() == [["a", "b"], ["c", "d"]]


def test_ocr_page_markdown_table():
    page = OcrPage(matrix=[["编号", "名称"], ["A001", "螺栓"]], engine=OcrEngineName.VL)
    lines = page.markdown_table().splitlines()
    assert lines[0] == "| 编号 | 名称 |"
    assert "| A001 | 螺栓 |" in lines


def test_parse_markdown_table_tolerates_fences_and_separator():
    text = "```markdown\n| 编号 | 名称 |\n| --- | --- |\n| A001 | 螺栓 |\n```"
    assert parse_markdown_table(text) == [["编号", "名称"], ["A001", "螺栓"]]


def test_parse_markdown_table_keeps_escaped_pipe_in_cell():
    # 单元格里的竖线被模型转义成 \|，不能当成列分隔符
    text = "| 规格 | 说明 |\n| --- | --- |\n| M8\\|20 | 螺栓 |"
    assert parse_markdown_table(text) == [["规格", "说明"], ["M8|20", "螺栓"]]


def test_parse_markdown_table_pads_ragged_rows():
    # 模型漏写一列时补齐，避免下游按行取列时越界
    text = "| 编号 | 名称 | 数量 |\n| --- | --- | --- |\n| A001 | 螺栓 |"
    assert parse_markdown_table(text) == [["编号", "名称", "数量"], ["A001", "螺栓", ""]]


def test_user_prompt_includes_column_hint():
    prompt = _user_prompt(["订单编号", "供应商"])
    assert "订单编号" in prompt and "供应商" in prompt
    assert "以图片内容为准" in prompt


def test_user_prompt_without_hint_is_generic():
    assert _user_prompt(None) == "请转写这张图片中的表格。"
    assert _user_prompt([]) == "请转写这张图片中的表格。"


def test_schema_column_hint_collects_labels_and_synonyms(purchase_order):
    hint = purchase_order.column_hint()
    assert "订单编号" in hint  # label
    assert "数量" in hint
    # 去重：同一名称不重复出现
    assert len(hint) == len(set(hint))


def test_fallback_keyvalue_parses_field_lines():
    text = "订单编号：PO-2024-001\n供应商: 华信机械\n备注"
    assert _fallback_keyvalue(text) == [
        ["订单编号", "PO-2024-001"],
        ["供应商", "华信机械"],
        ["备注", ""],
    ]
