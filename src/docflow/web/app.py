"""Web 界面：上传 → 处理 → 结果 → 人工复核 → 导出.

刻意保持「服务端渲染 + 少量表单」的形态：不需要前端构建链，`docflow serve` 一条命令就能演示，
也能直接作为内网工具使用。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from docflow import __version__
from docflow.config import Settings, get_settings
from docflow.errors import DocFlowError
from docflow.export import export_excel
from docflow.models.record import ReviewStatus
from docflow.models.schema import load_schemas
from docflow.pipeline import run_pipeline
from docflow.security import (
    AuditAction,
    AuditEvent,
    Permission,
    Principal,
    resolve_principal,
    verify_principal_signature,
)
from docflow.store import (
    apply_review,
    build_session_factory,
    get_record,
    get_run,
    list_audit,
    list_records,
    list_runs,
    load_run_result,
    record_audit,
    review_stats,
    save_run,
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.globals["version"] = __version__

logger = logging.getLogger("docflow.web")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    if settings.rbac_enabled and not settings.rbac_header_secret:
        logger.warning(
            "RBAC 已启用但未配置 DOCFLOW_RBAC_HEADER_SECRET：身份头可被客户端伪造。"
            "请仅在可信反向代理之后运行，或配置签名密钥。"
        )
    app.state.settings = settings
    app.state.session_factory = build_session_factory(settings)
    yield


app = FastAPI(title="DocFlow AI", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def limit_request_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """按 Content-Length 早拒超大请求体，避免 multipart 解析把整包 spool 到磁盘.

    单文件大小由上传处理逐块截断，这里兜住「多文件叠加」与「超大整体请求」——
    两层一起才真正兑现 `max_upload_bytes` / `max_request_bytes` 的承诺。
    """
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > settings.max_request_bytes:
        limit_mb = settings.max_request_bytes // (1024 * 1024)
        return Response(status_code=413, content=f"请求体超过上限 {limit_mb} MB")
    return await call_next(request)


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_principal(request: Request, settings: Settings = Depends(get_app_settings)) -> Principal:
    """从请求头解析操作者身份；RBAC 关闭时一律是 admin.

    身份头的可信度分三种情况处理，绝不「既没密钥又采信客户端头」：
    - `rbac_trust_proxy=True`：由可信反向代理注入并剥离外部身份头，直接采信；
    - 配置了 `DOCFLOW_RBAC_HEADER_SECRET`：身份头必须带合法 HMAC 签名，否则 401；
    - 两者都没有：无法验证身份头，一律降级到兜底角色（fail-closed），
      否则任何客户端都能靠伪造 `X-DocFlow-Role: admin` 越权。
    """
    user = request.headers.get("X-DocFlow-User")
    role = request.headers.get("X-DocFlow-Role")
    if settings.rbac_enabled:
        if settings.rbac_trust_proxy:
            pass
        elif settings.rbac_header_secret:
            signature = request.headers.get("X-DocFlow-Signature")
            if not verify_principal_signature(user, role, signature, settings.rbac_header_secret):
                raise HTTPException(status_code=401, detail="身份签名校验失败：X-DocFlow-Signature 缺失或无效")
        else:
            role = None
    return resolve_principal(
        user,
        role,
        enabled=settings.rbac_enabled,
        default_role=settings.rbac_default_role,
    )


def require(permission: Permission) -> Callable[..., Principal]:
    """生成一个「要求某权限」的依赖：不满足就 403，并记一条越权审计."""

    def dependency(
        request: Request,
        principal: Principal = Depends(get_principal),
        session: Session = Depends(get_session),
    ) -> Principal:
        if principal.can(permission):
            return principal
        record_audit(
            session,
            AuditEvent.of(
                principal,
                AuditAction.ACCESS_DENIED,
                target=permission.value,
                detail=request.url.path,
            ),
        )
        session.commit()
        raise HTTPException(
            status_code=403,
            detail=f"角色「{principal.role.value}」无权执行「{permission.value}」操作",
        )

    return dependency


# 模块级单例依赖：避免在参数默认值里调用工厂（也便于 FastAPI 复用依赖）
require_process = require(Permission.PROCESS)
require_review = require(Permission.REVIEW)
require_export = require(Permission.EXPORT)
require_admin = require(Permission.ADMIN)


# ── 首页：上传 + 最近运行 + 复核概览 ─────────────────────────
@app.get("/")
def index(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(get_principal),
) -> Any:
    schemas = load_schemas(settings.schemas_dir)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active": "home",
            "schemas": [schemas[name] for name in sorted(schemas)],
            "runs": list_runs(session, limit=10),
            "stats": review_stats(session),
            "ocr_enabled": settings.ocr_enabled,
            "vl_configured": settings.vl_configured,
            "rbac_enabled": settings.rbac_enabled,
            "principal": principal,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/process")
async def process(
    request: Request,
    schema_name: str = Form(...),
    files: list[UploadFile] = Form(...),
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(require_process),
) -> RedirectResponse:
    schemas = load_schemas(settings.schemas_dir)
    schema = schemas.get(schema_name)
    if schema is None:
        return _back(f"未知的 Schema：{schema_name}")

    saved: list[Path] = []
    limit_mb = settings.max_upload_bytes // (1024 * 1024)
    for upload in files:
        if not upload.filename:
            continue
        # 只取文件名，杜绝 ../ 之类的路径穿越
        target = settings.upload_dir / Path(upload.filename).name
        # 分块落盘并累计大小：既不把整份文件读进内存，也避免超大文件写满磁盘
        size = 0
        oversize = False
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    oversize = True
                    break
                handle.write(chunk)
        if oversize:
            target.unlink(missing_ok=True)
            return _back(f"文件「{Path(upload.filename).name}」超过大小上限 {limit_mb} MB")
        saved.append(target)

    if not saved:
        return _back("没有收到任何文件")

    try:
        result = run_pipeline(schema, settings, saved)
    except DocFlowError as exc:
        return _back(f"处理失败：{exc}")

    factory = request.app.state.session_factory
    with factory() as session:
        save_run(session, result)
        record_audit(
            session,
            AuditEvent.of(
                principal,
                AuditAction.PROCESS,
                target=result.run_id,
                detail=f"schema={result.schema_name} files={len(saved)} records={result.total_records}",
            ),
        )
        session.commit()

    return RedirectResponse(url=f"/runs/{result.run_id}", status_code=303)


def _back(message: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(url=f"/?error={quote(message)}", status_code=303)


# ── 运行详情 ─────────────────────────────────────────────────
@app.get("/runs/{run_id}")
def run_detail(
    request: Request,
    run_id: str,
    status: str | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(get_principal),
) -> Any:
    run = get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录：{run_id}")

    status_filter = None
    if status:
        try:
            status_filter = ReviewStatus(status)
        except ValueError:
            status_filter = None

    records = list_records(session, run_id=run_id, status=status_filter)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "active": "home",
            "run": run,
            "records": records,
            "field_keys": _field_keys(settings, run.schema_name, records),
            "status_filter": status or "",
            "statuses": [status.value for status in ReviewStatus],
            "principal": principal,
        },
    )


def _field_keys(settings: Settings, schema_name: str, records: list[Any]) -> list[str]:
    """记录表要展示哪些列：优先按 Schema 定义顺序，未定义的字段补在后面."""
    keys: list[str] = []
    schema = load_schemas(settings.schemas_dir).get(schema_name)
    if schema is not None:
        keys = list(schema.field_keys())
    seen = set(keys)
    for record in records:
        for key in record.payload or {}:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


@app.post("/runs/{run_id}/records/{record_id}/review")
def review_record(
    request: Request,
    run_id: str,
    record_id: str,
    action: str = Form(...),
    note: str = Form(""),
    reviewer: str = Form(""),
    next_url: str = Form("/review"),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_review),
) -> RedirectResponse:
    if action not in (ReviewStatus.APPROVED.value, ReviewStatus.REJECTED.value, ReviewStatus.PENDING.value):
        raise HTTPException(status_code=400, detail=f"不支持的复核动作：{action}")
    if get_record(session, run_id, record_id) is None:
        raise HTTPException(status_code=404, detail=f"未找到记录：{run_id}/{record_id}")

    actor = reviewer.strip() or principal.name
    apply_review(session, run_id, record_id, ReviewStatus(action), reviewer=actor, note=note)
    record_audit(
        session,
        AuditEvent.of(principal, AuditAction.REVIEW, target=f"{run_id}/{record_id}", detail=f"action={action}"),
    )
    session.commit()
    return RedirectResponse(url=_safe_next(next_url), status_code=303)


def _safe_next(url: str) -> str:
    """只允许站内相对路径跳转，挡掉 `//evil.com`、`https://evil.com` 这类开放重定向."""
    candidate = (url or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//") and "\\" not in candidate:
        return candidate
    return "/review"


# ── 待复核队列 ───────────────────────────────────────────────
@app.get("/review")
def review_queue(
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> Any:
    records = list_records(session, status=ReviewStatus.PENDING)
    return templates.TemplateResponse(
        request,
        "review.html",
        {"active": "review", "records": records, "stats": review_stats(session), "principal": principal},
    )


# ── 审计日志（仅 admin）──────────────────────────────────────
@app.get("/audit")
def audit_log(
    request: Request,
    limit: int = 100,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
) -> Any:
    rows = list_audit(session, limit=limit)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {"active": "audit", "rows": rows, "principal": principal, "limit": limit},
    )


# ── 知识检索（RAG，只读，需要 VIEW 权限）─────────────────────
@app.get("/search")
def search_page(
    request: Request,
    q: str = "",
    run_id: str | None = None,
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(get_principal),
) -> Any:
    from docflow.retrieval import RetrievalStore

    store = RetrievalStore(settings.retrieval_dir)
    query = q.strip()
    hits = store.search_all(query, top_k=settings.rag_top_k, run_id=run_id) if query else []
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "active": "search",
            "q": q,
            "run_id": run_id or "",
            "hits": hits,
            "indexed_runs": store.run_ids(),
            "llm_configured": settings.rag_llm_configured,
            "principal": principal,
        },
    )


# ── 导出下载 ─────────────────────────────────────────────────
@app.get("/runs/{run_id}/export.xlsx")
def download_excel(
    run_id: str,
    settings: Settings = Depends(get_app_settings),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_export),
) -> FileResponse:
    result = load_run_result(session, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录：{run_id}")
    schema = load_schemas(settings.schemas_dir).get(result.schema_name)
    target = settings.export_dir / f"{run_id}.xlsx"
    export_excel(result, target, schema)
    record_audit(
        session,
        AuditEvent.of(principal, AuditAction.EXPORT, target=run_id, detail=f"records={result.total_records}"),
    )
    session.commit()
    return FileResponse(
        target,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"docflow-{run_id}.xlsx",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
