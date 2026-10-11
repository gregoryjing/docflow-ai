"""共享测试夹具：临时工作目录 + 真实 Schema 模板 + 样例文件."""

from __future__ import annotations

from pathlib import Path

import pytest

from docflow.config import Settings
from docflow.models.schema import SchemaDefinition, load_schemas

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"

ORDER_CSV = (
    "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期,交付日期,是否加急\n"
    "PO-2024-001,华信机械,A001,螺栓,100,0.5,50,2024-03-01,2024-03-15,否\n"
    "PO-2024-002,远大五金,B002,齿轮,20,12.5,250,2024-03-02,2024-03-20,否\n"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """每个测试独立的 data 目录，避免互相污染；Schema 用仓库里的真实模板.

    显式关闭 OCR / VL / RBAC，避免本机 .env 或环境变量泄漏到测试环境。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        schemas_dir=SCHEMAS_DIR,
        ocr_enabled=False,
        vl_base_url=None,
        vl_model=None,
        rbac_enabled=False,
    )


@pytest.fixture
def ocr_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=data_dir, schemas_dir=SCHEMAS_DIR, ocr_enabled=True)


@pytest.fixture
def schemas() -> dict[str, SchemaDefinition]:
    return load_schemas(SCHEMAS_DIR)


@pytest.fixture
def purchase_order(schemas: dict[str, SchemaDefinition]) -> SchemaDefinition:
    return schemas["purchase_order"]


@pytest.fixture
def order_csv(tmp_path: Path) -> Path:
    path = tmp_path / "orders.csv"
    path.write_text(ORDER_CSV, encoding="utf-8")
    return path


@pytest.fixture
def write_csv(tmp_path: Path):
    """按名字写一个 UTF-8 CSV，返回路径."""

    def _write(name: str, content: str) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write
