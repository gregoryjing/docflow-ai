from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from docflow.security.rbac import Role


class Settings(BaseSettings):
    """全局配置，优先级：环境变量 > .env > 默认值."""

    model_config = SettingsConfigDict(
        env_prefix="DOCFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"), description="工作目录：上传件、产物、数据库都放这里")
    schemas_dir: Path = Field(default=Path("schemas"), description="Schema 模板目录")
    pipelines_dir: Path = Field(default=Path("pipelines"), description="Pipeline 规范目录（*.toml）")
    database_url: str = Field(default="", description="数据库连接串，留空则用 data_dir 下的 SQLite")

    # ── 管线执行 ──────────────────────────────────────────────
    checkpoint_enabled: bool = Field(
        default=False, description="是否默认启用步骤级断点续跑（CLI 传 --checkpoint 可单独开启）"
    )

    # ── 访问控制（RBAC）───────────────────────────────────────
    rbac_enabled: bool = Field(default=False, description="是否启用 RBAC 权限校验；关闭时所有请求视为 admin")
    rbac_default_role: Role = Field(default=Role.VIEWER, description="请求未携带角色时的兜底角色（RBAC 开启时生效）")
    rbac_header_secret: str | None = Field(
        default=None,
        description=(
            "身份头签名密钥（HMAC-SHA256）。设置后，X-DocFlow-User/Role 必须附带合法的 "
            "X-DocFlow-Signature，否则 401——防止客户端直接伪造 admin。"
        ),
    )
    rbac_trust_proxy: bool = Field(
        default=False,
        description=(
            "是否信任由反向代理注入的身份头（可信代理模式）。"
            "为 True 时才直接采信 X-DocFlow-User/Role；否则必须配置 rbac_header_secret 做签名校验，"
            "两者都没有时一律降级为兜底角色（fail-closed），避免伪造身份头提权。"
        ),
    )

    # ── Web 上传 ──────────────────────────────────────────────
    max_upload_bytes: int = Field(
        default=25 * 1024 * 1024, ge=1024, description="单个上传文件的大小上限（字节），防止超大文件打爆内存"
    )
    max_request_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        description="单次上传请求体的上限（字节），按 Content-Length 早拒；建议同时在反向代理设 client_max_body_size",
    )

    # ── 字段映射 ──────────────────────────────────────────────
    fuzzy_threshold: float = Field(default=0.75, ge=0.0, le=1.0, description="模糊匹配接受阈值，低于此值视为未映射")
    fuzzy_suggest_threshold: float = Field(
        default=0.55, ge=0.0, le=1.0, description="模糊匹配建议阈值，落在 [suggest, accept) 的映射进人工复核"
    )

    # ── 置信度分流（Human-in-the-loop）────────────────────────
    auto_approve_threshold: float = Field(default=0.90, ge=0.0, le=1.0, description="置信度 ≥ 此值：自动通过，直接入库")
    review_threshold: float = Field(
        default=0.70, ge=0.0, le=1.0, description="置信度 < 此值：强制人工复核；介于两者之间：建议检查"
    )

    # ── 表格解析 ──────────────────────────────────────────────
    max_header_scan_rows: int = Field(default=10, ge=1, le=50, description="自动识别表头时向下扫描的最大行数")
    header_min_fill_ratio: float = Field(default=0.6, ge=0.0, le=1.0, description="表头行的最小非空比例")

    # ── 文档解析 ──────────────────────────────────────────────
    pdf_text_min_chars: int = Field(default=40, ge=0, description="PDF 文本层字符数低于此值判定为扫描件（走 OCR）")
    pdf_render_dpi: int = Field(default=200, ge=72, le=600, description="扫描 PDF 转图片的渲染 DPI")
    document_backend: Literal["builtin", "docling"] = Field(
        default="builtin",
        description="PDF/Word 解析后端：builtin=PyMuPDF/python-docx；docling=Docling 高级版面分析（需额外依赖）",
    )
    docling_do_ocr: bool = Field(default=False, description="Docling 是否对扫描件做 OCR（需 Docling 的 OCR 依赖）")

    # ── OCR（M2 启用）─────────────────────────────────────────
    ocr_enabled: bool = Field(default=False, description="是否启用 OCR 链路")
    ocr_min_confidence: float = Field(
        default=0.85, ge=0.0, le=1.0, description="一级 OCR 平均置信度低于此值则升级到 VL 模型"
    )
    vl_base_url: str | None = Field(default=None, description="本地 VL 模型的 OpenAI 兼容地址")
    vl_api_key: str | None = Field(default=None, description="本地 VL 模型密钥（Ollama 填任意值）")
    vl_model: str | None = Field(default=None, description="本地 VL 模型名，如 qwen3-vl:8b")
    vl_timeout_seconds: float = Field(default=180.0, ge=1.0)

    # ── L3 字段映射（LLM 兜底，默认关闭）─────────────────────
    llm_mapping_enabled: bool = Field(default=False, description="是否启用 L3 LLM 字段映射（仅模糊匹配失败时调用）")
    llm_base_url: str | None = Field(default=None, description="文本 LLM 的 OpenAI 兼容地址")
    llm_api_key: str | None = Field(default=None, description="文本 LLM 密钥（Ollama 填任意值）")
    llm_model: str | None = Field(default=None, description="文本 LLM 模型名")
    llm_timeout_seconds: float = Field(default=60.0, ge=1.0)

    # ── 知识检索（RAG，第二阶段）──────────────────────────────
    rag_enabled: bool = Field(default=False, description="是否启用知识检索：对已处理数据建索引并支持问答")
    rag_top_k: int = Field(default=5, ge=1, le=50, description="检索返回的片段数")
    rag_chunk_chars: int = Field(default=600, ge=100, le=4000, description="单个检索片段的最大字符数")

    @model_validator(mode="after")
    def _check_thresholds(self) -> Settings:
        if self.review_threshold > self.auto_approve_threshold:
            raise ValueError("review_threshold 不能大于 auto_approve_threshold")
        if self.fuzzy_suggest_threshold > self.fuzzy_threshold:
            raise ValueError("fuzzy_suggest_threshold 不能大于 fuzzy_threshold")
        return self

    # ── 派生路径 ──────────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return self.data_dir / "docflow.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.db_path.as_posix()}"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def checkpoint_dir(self) -> Path:
        return self.data_dir / "state" / "checkpoints"

    @property
    def retrieval_dir(self) -> Path:
        return self.data_dir / "state" / "retrieval"

    @property
    def vl_configured(self) -> bool:
        return bool(self.vl_base_url and self.vl_model)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_mapping_enabled and self.llm_base_url and self.llm_model)

    @property
    def rag_llm_configured(self) -> bool:
        """RAG 问答复用文本 LLM 的地址与模型名（不依赖 L3 映射开关）."""
        return bool(self.llm_base_url and self.llm_model)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.artifact_dir, self.export_dir, self.retrieval_dir):
            path.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """每次调用读取新配置，便于测试与长跑进程中的配置刷新."""
    return Settings()
