"""docflow 命令行入口.

定位：把整条链路（接入 → 映射 → 质检 → 分流 → 落库 → 导出）用一条命令跑通，
同时提供复核与运维子命令，方便在没有 Web 的环境里也能完整使用。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from docflow import __version__
from docflow.batch import BatchProcessor, BatchReport
from docflow.config import Settings, get_settings
from docflow.errors import DocFlowError
from docflow.export import export_auto
from docflow.models.record import ReviewStatus
from docflow.models.schema import SchemaDefinition, load_schemas
from docflow.pipeline import PipelineSpec, run_pipeline
from docflow.pipeline.report import PipelineResult
from docflow.pipeline.spec import DEFAULT_SPEC_NAME, load_specs, resolve_spec
from docflow.security import AuditAction, AuditEvent, Principal, Role

app = typer.Typer(
    help="DocFlow AI：企业文档智能处理（多格式接入 + 字段映射 + 质检 + 人工复核）",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_LEVEL_STYLE = {"error": "red", "warning": "yellow", "info": "cyan"}

# 多个子命令共用的选项类型别名，避免逐处重复且保持行宽
PipelineName = Annotated[
    str,
    typer.Option("--pipeline", "-P", help="使用的 Pipeline 规范名，见 docflow pipelines"),
]
CheckpointDir = Annotated[
    Path | None,
    typer.Option("--checkpoint", help="启用步骤级断点续跑，检查点写入该目录"),
]


def _load_schema_or_exit(settings: Settings, name: str) -> SchemaDefinition:
    schemas = load_schemas(settings.schemas_dir)
    if not schemas:
        console.print(f"[red]没有找到任何 Schema[/red]（目录：{settings.schemas_dir}）")
        raise typer.Exit(code=1)
    schema = schemas.get(name)
    if schema is None:
        console.print(f"[red]未知 Schema：{name}[/red]，可用：{', '.join(sorted(schemas))}")
        raise typer.Exit(code=1)
    return schema


def _load_spec_or_exit(settings: Settings, name: str) -> PipelineSpec:
    try:
        return resolve_spec(settings.pipelines_dir, name)
    except DocFlowError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc


def _write_audit(settings: Settings, actor: str, action: AuditAction, target: str, detail: str = "") -> None:
    """CLI 侧写一条审计流水。CLI 是单机可信环境，操作者一律按 admin 记录."""
    from docflow.store import build_session_factory, record_audit

    principal = Principal(name=actor or "cli", role=Role.ADMIN)
    factory = build_session_factory(settings)
    with factory() as session:
        record_audit(session, AuditEvent.of(principal, action, target=target, detail=detail))
        session.commit()


def _render_result(result: PipelineResult, preview: int) -> None:
    counts = result.issue_counts
    summary = Table(title=f"处理结果 · {result.schema_label or result.schema_name}", show_header=False)
    summary.add_column("指标", style="bold")
    summary.add_column("值")
    summary.add_row("运行 ID", result.run_id)
    summary.add_row("耗时", f"{result.duration_seconds:.2f}s")
    summary.add_row("管线", result.pipeline_name)
    if result.resumed_from:
        summary.add_row("续跑", f"[cyan]从「{result.resumed_from}」之后恢复[/cyan]")
    summary.add_row("记录数", str(result.total_records))
    summary.add_row("自动通过", f"[green]{result.auto_approved}[/green]")
    summary.add_row("待复核", f"[yellow]{result.review_pending}[/yellow]")
    summary.add_row(
        "问题",
        f"[red]{counts['error']} 错误[/red] / [yellow]{counts['warning']} 警告[/yellow] / "
        f"[cyan]{counts['info']} 提示[/cyan]",
    )
    console.print(summary)

    if result.sources:
        table = Table(title="来源明细")
        for column in ("来源", "记录", "待复核", "状态"):
            table.add_column(column)
        for source in result.sources:
            style = {"ok": "green", "warning": "yellow", "error": "red"}.get(source.status, "white")
            table.add_row(
                source.source.label(),
                str(source.record_count),
                str(source.review_pending),
                f"[{style}]{source.status}[/{style}]",
            )
        console.print(table)

    for failure in result.failures:
        console.print(f"[red]解析失败[/red] {escape(failure.file_name)}：{escape(failure.error)}")

    for warning in result.file_warnings:
        console.print(f"[yellow]无数据产出[/yellow] {escape(warning.file_name)}：{escape(warning.message)}")

    if result.dedupe.groups:
        dedupe_table = Table(title=f"去重组（{result.duplicate_groups} 组 / 冗余 {result.duplicate_records} 条）")
        for column in ("模式", "实体标识", "记录数", "相似度", "需人工确认"):
            dedupe_table.add_column(column, overflow="fold")
        for group in result.dedupe.groups:
            mode = "模糊" if group.mode == "fuzzy" else "精确"
            style = "yellow" if group.review_required else "cyan"
            dedupe_table.add_row(
                mode,
                group.key_value,
                str(group.size),
                f"{group.similarity:.2f}",
                f"[{style}]{'是' if group.review_required else '否'}[/{style}]",
            )
        console.print(dedupe_table)

    if preview > 0 and result.records:
        records_table = Table(title=f"记录预览（前 {min(preview, len(result.records))} 条）")
        for column in ("状态", "置信度", "字段取值", "问题"):
            records_table.add_column(column, overflow="fold")
        for record in result.records[:preview]:
            flat = record.to_flat()
            status_style = "green" if record.review_status is ReviewStatus.AUTO_APPROVED else "yellow"
            body = ", ".join(f"{key}={value}" for key, value in flat.items() if value is not None)
            problems = "; ".join(issue.message for issue in record.issues[:3])
            records_table.add_row(
                f"[{status_style}]{record.review_status.value}[/{status_style}]",
                f"{record.min_confidence:.2f}",
                body or "[dim]（空）[/dim]",
                escape(problems) if problems else "[dim]-[/dim]",
            )
        console.print(records_table)


@app.command()
def version() -> None:
    """输出版本号."""
    console.print(f"docflow-ai {__version__}")


@app.command("schemas")
def list_schemas() -> None:
    """列出所有可用的 Schema 模板."""
    settings = get_settings()
    schemas = load_schemas(settings.schemas_dir)
    if not schemas:
        console.print(f"[yellow]Schema 目录为空：{settings.schemas_dir}[/yellow]")
        raise typer.Exit(code=1)
    table = Table(title=f"Schema 模板（{settings.schemas_dir}）")
    for column in ("名称", "标题", "字段数", "必填", "主键"):
        table.add_column(column)
    for name in sorted(schemas):
        schema = schemas[name]
        table.add_row(
            name,
            schema.label,
            str(len(schema.fields)),
            str(len(schema.required_keys())),
            schema.primary_key or "-",
        )
    console.print(table)


@app.command("pipelines")
def list_pipelines() -> None:
    """列出所有可用的 Pipeline 规范（含内置的 default）."""
    settings = get_settings()
    specs = load_specs(settings.pipelines_dir)
    table = Table(title=f"Pipeline 规范（{settings.pipelines_dir}）")
    for column in ("名称", "标题", "步骤", "断点续跑"):
        table.add_column(column, overflow="fold")
    builtin = resolve_spec(settings.pipelines_dir, DEFAULT_SPEC_NAME)
    rows = {DEFAULT_SPEC_NAME: builtin, **specs}
    for name in sorted(rows):
        spec = rows[name]
        steps = " → ".join(spec.active_steps())
        table.add_row(name, spec.label or "-", steps, "支持" if spec.checkpoint else "关闭")
    console.print(table)


@app.command()
def run(
    schema: Annotated[str, typer.Argument(help="使用的 Schema 名称，见 docflow schemas")],
    paths: Annotated[list[Path], typer.Argument(help="待处理的文件或目录")],
    pipeline: PipelineName = DEFAULT_SPEC_NAME,
    checkpoint: CheckpointDir = None,
    save: Annotated[bool, typer.Option("--save/--no-save", help="是否落库")] = True,
    export: Annotated[Path | None, typer.Option("--export", "-o", help="导出路径（.xlsx 或目录）")] = None,
    csv_export: Annotated[bool, typer.Option("--csv", help="导出为 CSV 目录而非 Excel")] = False,
    preview: Annotated[int, typer.Option("--preview", help="预览前 N 条记录")] = 8,
    actor: Annotated[str, typer.Option("--actor", help="操作者标识，写入审计日志")] = "cli",
) -> None:
    """按 Schema 处理一批文件：接入 → 映射 → 质检 → 分流 →（落库 / 导出）."""
    settings = get_settings()
    settings.ensure_dirs()
    schema_def = _load_schema_or_exit(settings, schema)
    spec = _load_spec_or_exit(settings, pipeline)
    files = _expand_paths(paths)
    if not files:
        console.print("[red]没有找到任何可处理的文件[/red]")
        raise typer.Exit(code=1)

    try:
        result = run_pipeline(schema_def, settings, files, spec=spec, checkpoint_dir=checkpoint)
    except DocFlowError as exc:
        console.print(f"[red]处理失败：{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc

    _render_result(result, preview)

    if save:
        from docflow.store import build_session_factory, record_audit, save_run

        # 落库与审计放同一事务：避免「有运行、无审计」的中间态
        principal = Principal(name=actor or "cli", role=Role.ADMIN)
        factory = build_session_factory(settings)
        with factory() as session:
            save_run(session, result)
            record_audit(
                session,
                AuditEvent.of(
                    principal,
                    AuditAction.PROCESS,
                    target=result.run_id,
                    detail=f"schema={result.schema_name} files={len(files)} records={result.total_records}",
                ),
            )
            session.commit()
        console.print(f"[green]已落库[/green] 运行 ID：{result.run_id}")

    if export is not None:
        target = _export(result, export, csv_export, schema_def)
        console.print(f"[green]已导出[/green] {target}")


def _expand_paths(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(item for item in path.iterdir() if item.is_file()))
        elif path.exists():
            files.append(path)
        else:
            console.print(f"[yellow]跳过不存在的路径：{path}[/yellow]")
    return files


def _export(
    result: PipelineResult,
    target: Path,
    as_csv: bool,
    schema: SchemaDefinition | None,
) -> Path:
    return export_auto(result, target, schema, as_csv=as_csv)


@app.command()
def runs(
    limit: Annotated[int, typer.Option("--limit", help="最多展示多少条")] = 20,
) -> None:
    """查看历史运行记录."""
    settings = get_settings()
    from docflow.store import build_session_factory, list_runs

    factory = build_session_factory(settings)
    with factory() as session:
        rows = list_runs(session, limit=limit)
    if not rows:
        console.print("[yellow]暂无运行记录[/yellow]")
        return
    table = Table(title="历史运行")
    for column in ("运行 ID", "Schema", "记录", "错误", "待复核", "开始时间"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.run_id,
            row.schema_name,
            str(row.record_count),
            f"[red]{row.error_count}[/red]" if row.error_count else "0",
            f"[yellow]{row.pending_count}[/yellow]" if row.pending_count else "0",
            row.started_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    console.print(table)


@app.command()
def show(run_id: Annotated[str, typer.Argument(help="运行 ID")]) -> None:
    """查看某次运行的详情."""
    settings = get_settings()
    from docflow.store import build_session_factory, load_run_result

    factory = build_session_factory(settings)
    with factory() as session:
        result = load_run_result(session, run_id)
    if result is None:
        console.print(f"[red]未找到运行记录：{run_id}[/red]")
        raise typer.Exit(code=1)
    _render_result(result, preview=5)


@app.command()
def pending(
    run_id: Annotated[str | None, typer.Option("--run-id", help="限定某次运行")] = None,
    limit: Annotated[int, typer.Option("--limit", help="最多展示多少条")] = 20,
) -> None:
    """列出待人工复核的记录."""
    settings = get_settings()
    from docflow.store import build_session_factory, list_records

    factory = build_session_factory(settings)
    with factory() as session:
        rows = list_records(session, run_id=run_id, status=ReviewStatus.PENDING, limit=limit)
    if not rows:
        console.print("[green]没有待复核记录[/green]")
        return
    table = Table(title="待复核记录")
    for column in ("记录 ID", "来源", "行号", "置信度", "问题"):
        table.add_column(column, overflow="fold")
    for row in rows:
        problems = "; ".join(issue.get("message", "") for issue in (row.issues or [])[:2])
        table.add_row(
            row.record_id,
            f"{row.source_file}#{row.source_location}",
            str((row.row_index or 0) + 1),
            f"{row.min_confidence:.2f}",
            problems or "-",
        )
    console.print(table)


@app.command()
def review(
    record_id: Annotated[str, typer.Argument(help="记录 ID（见 docflow pending）")],
    run_id: Annotated[
        str | None, typer.Option("--run-id", help="记录所属运行 ID；同一指纹跨运行存在时必须指定")
    ] = None,
    approve: Annotated[bool, typer.Option("--approve/--reject", help="通过还是驳回")] = True,
    note: Annotated[str, typer.Option("--note", help="复核备注")] = "",
    reviewer: Annotated[str, typer.Option("--reviewer", help="复核人")] = "",
) -> None:
    """人工复核：通过或驳回一条记录."""
    settings = get_settings()
    from docflow.store import apply_review, build_session_factory, find_records_by_id, record_audit

    action = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
    factory = build_session_factory(settings)
    with factory() as session:
        if run_id is None:
            matches = find_records_by_id(session, record_id)
            if not matches:
                console.print(f"[red]未找到记录：{record_id}[/red]")
                raise typer.Exit(code=1)
            if len(matches) > 1:
                runs = ", ".join(sorted({row.run_id for row in matches}))
                console.print(f"[yellow]该记录 ID 在多次运行中出现，请用 --run-id 指定：{runs}[/yellow]")
                raise typer.Exit(code=1)
            run_id = matches[0].run_id
        record = apply_review(session, run_id, record_id, action, reviewer=reviewer, note=note)
        record_audit(
            session,
            AuditEvent.of(
                Principal(name=reviewer or "cli", role=Role.ADMIN),
                AuditAction.REVIEW,
                target=f"{run_id}/{record_id}",
                detail=f"action={action.value}",
            ),
        )
        session.commit()
    if record is None:
        console.print(f"[red]未找到记录：{run_id}/{record_id}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]已{action.value}[/green] 记录 {record_id}（运行 {run_id}）")


@app.command()
def audit(
    limit: Annotated[int, typer.Option("--limit", help="最多展示多少条")] = 50,
    action: Annotated[
        str | None, typer.Option("--action", help="只看某类动作：process/review/export/access_denied")
    ] = None,
) -> None:
    """查看审计日志：谁在什么时候对什么做了什么."""
    settings = get_settings()
    from docflow.store import build_session_factory, list_audit

    factory = build_session_factory(settings)
    with factory() as session:
        rows = list_audit(session, limit=limit, action=action)
    if not rows:
        console.print("[yellow]暂无审计记录[/yellow]")
        return
    table = Table(title="审计日志")
    for column in ("时间", "操作者", "角色", "动作", "目标", "详情"):
        table.add_column(column, overflow="fold")
    for row in rows:
        style = "red" if row.action == "access_denied" else "green"
        table.add_row(
            row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            row.actor or "-",
            row.role or "-",
            f"[{style}]{row.action}[/{style}]",
            row.target or "-",
            row.detail or "-",
        )
    console.print(table)


@app.command()
def export(
    run_id: Annotated[str, typer.Argument(help="运行 ID")],
    out: Annotated[Path, typer.Option("--out", "-o", help="导出路径（.xlsx 或目录）")],
    csv_export: Annotated[bool, typer.Option("--csv", help="导出为 CSV 目录而非 Excel")] = False,
) -> None:
    """重新导出某次运行的结果（不重跑处理）."""
    settings = get_settings()
    from docflow.store import build_session_factory, load_run_result

    factory = build_session_factory(settings)
    with factory() as session:
        result = load_run_result(session, run_id)
    if result is None:
        console.print(f"[red]未找到运行记录：{run_id}[/red]")
        raise typer.Exit(code=1)

    schema = load_schemas(settings.schemas_dir).get(result.schema_name)
    if schema is None:
        console.print(f"[yellow]Schema「{result.schema_name}」已不存在，将按记录实际字段导出[/yellow]")
    target = _export(result, out, csv_export, schema)
    console.print(f"[green]已导出[/green] {target}")


@app.command()
def index(run_id: Annotated[str, typer.Argument(help="运行 ID，见 docflow runs")]) -> None:
    """为某次运行建立检索索引（知识检索 / 问答的前提）."""
    settings = get_settings()
    settings.ensure_dirs()
    from docflow.retrieval import RetrievalIndex, RetrievalStore
    from docflow.store import build_session_factory, get_run, list_records

    factory = build_session_factory(settings)
    with factory() as session:
        if get_run(session, run_id) is None:
            console.print(f"[red]未找到运行记录：{run_id}[/red]")
            raise typer.Exit(code=1)
        rows = list_records(session, run_id=run_id)

    built = RetrievalIndex.build(run_id, rows, max_chars=settings.rag_chunk_chars)
    target = RetrievalStore(settings.retrieval_dir).save(built)
    console.print(f"[green]已建立索引[/green] 片段数 {len(built.chunks)} → {target}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="检索关键词，如「螺栓 华信机械」")],
    run_id: Annotated[str | None, typer.Option("--run-id", help="限定某次运行的索引")] = None,
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="返回片段数")] = 5,
) -> None:
    """在已建索引的运行里做统一检索（纯 Python BM25，跨来源）."""
    settings = get_settings()
    from docflow.retrieval import RetrievalStore

    store = RetrievalStore(settings.retrieval_dir)
    if not store.run_ids():
        console.print("[yellow]还没有任何索引，请先执行 docflow index <run_id>[/yellow]")
        raise typer.Exit(code=1)
    hits = store.search_all(query, top_k=top_k, run_id=run_id)
    if not hits:
        console.print("[yellow]没有匹配结果[/yellow]")
        return
    table = Table(title=f"检索：{query}")
    for column in ("得分", "来源", "内容"):
        table.add_column(column, overflow="fold")
    for hit in hits:
        table.add_row(f"{hit.score:.3f}", hit.chunk.citation(), hit.chunk.text.replace("\n", " | ")[:120])
    console.print(table)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="自然语言问题")],
    run_id: Annotated[str | None, typer.Option("--run-id", help="限定某次运行的索引")] = None,
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="检索片段数")] = 5,
) -> None:
    """基于已建索引回答问题：配置文本 LLM 则生成带引用回答，否则给出证据片段."""
    settings = get_settings()
    from docflow.retrieval import RagAnswerer, RetrievalStore

    hits = RetrievalStore(settings.retrieval_dir).search_all(question, top_k=top_k, run_id=run_id)
    answer = RagAnswerer(settings).answer(question, hits)
    console.print(f"[bold]问题[/bold] {escape(question)}")
    console.print(f"[bold]回答[/bold] {escape(answer.answer)}")
    if answer.citations:
        table = Table(title="引用片段")
        for column in ("编号", "来源", "内容"):
            table.add_column(column, overflow="fold")
        for number, hit in enumerate(answer.citations, 1):
            table.add_row(str(number), hit.chunk.citation(), hit.chunk.text.replace("\n", " | ")[:120])
        console.print(table)
    if not answer.generated:
        console.print(
            "[yellow]提示：未配置文本 LLM，以上为抽取式证据；"
            "配置 DOCFLOW_LLM_BASE_URL / DOCFLOW_LLM_MODEL 可生成回答[/yellow]"
        )


def _render_batch(report: BatchReport, preview: int) -> None:
    """批量/定时任务的每轮摘要."""
    summary = Table(title="批处理结果", show_header=False)
    summary.add_column("指标", style="bold")
    summary.add_column("值")
    summary.add_row("发现文件", str(len(report.candidates)))
    summary.add_row("本轮处理", f"[green]{len(report.processed)}[/green]")
    summary.add_row("跳过（已处理）", f"[cyan]{len(report.skipped)}[/cyan]")
    summary.add_row("失败（留待重试）", f"[red]{len(report.failed)}[/red]" if report.failed else "0")
    if report.result is not None:
        summary.add_row("运行 ID", report.result.run_id)
        summary.add_row("记录数", str(report.result.total_records))
        summary.add_row("待复核", f"[yellow]{report.result.review_pending}[/yellow]")
    if report.export_path is not None:
        summary.add_row("导出", str(report.export_path))
    console.print(summary)

    if report.error:
        console.print(f"[red]错误[/red]：{escape(report.error)}")
    for path in report.failed:
        console.print(f"[red]解析失败[/red] {escape(path.name)}")
    if preview > 0 and report.result is not None:
        _render_result(report.result, preview)


@app.command()
def batch(
    schema: Annotated[str, typer.Argument(help="使用的 Schema 名称，见 docflow schemas")],
    inbox: Annotated[Path, typer.Argument(help="收件目录：批量扫描其中的文件")],
    pipeline: PipelineName = DEFAULT_SPEC_NAME,
    checkpoint: CheckpointDir = None,
    pattern: Annotated[list[str] | None, typer.Option("--pattern", "-p", help="文件名通配符，可重复")] = None,
    recursive: Annotated[bool, typer.Option("--recursive", "-r", help="递归扫描子目录")] = False,
    archive: Annotated[Path | None, typer.Option("--archive", help="处理成功后把源文件移动到该目录")] = None,
    export: Annotated[Path | None, typer.Option("--export", "-o", help="导出路径（.xlsx 或目录）")] = None,
    csv_export: Annotated[bool, typer.Option("--csv", help="导出为 CSV 目录而非 Excel")] = False,
    reprocess: Annotated[bool, typer.Option("--reprocess", help="忽略去重账本，强制重跑已处理文件")] = False,
    preview: Annotated[int, typer.Option("--preview", help="预览前 N 条记录")] = 0,
    actor: Annotated[str, typer.Option("--actor", help="操作者标识，写入审计日志")] = "cli",
) -> None:
    """批量处理收件目录：按内容指纹跳过已处理文件，可选归档与导出."""
    settings = get_settings()
    settings.ensure_dirs()
    schema_def = _load_schema_or_exit(settings, schema)
    spec = _load_spec_or_exit(settings, pipeline)

    processor = BatchProcessor(schema_def, settings, spec=spec, checkpoint_dir=checkpoint)
    report = processor.process(
        inbox,
        patterns=pattern or ("*",),
        recursive=recursive,
        archive=archive,
        export=export,
        as_csv=csv_export,
        reprocess=reprocess,
    )
    _render_batch(report, preview)
    if report.result is not None:
        _write_audit(
            settings,
            actor,
            AuditAction.PROCESS,
            target=report.result.run_id,
            detail=f"batch processed={len(report.processed)} skipped={len(report.skipped)}",
        )


@app.command()
def watch(
    schema: Annotated[str, typer.Argument(help="使用的 Schema 名称，见 docflow schemas")],
    inbox: Annotated[Path, typer.Argument(help="收件目录：定时轮询其中的新文件")],
    interval: Annotated[float, typer.Option("--interval", "-i", help="轮询间隔（秒）", min=1.0)] = 60.0,
    once: Annotated[bool, typer.Option("--once", help="只跑一轮就退出（便于交给系统计划任务）")] = False,
    pipeline: PipelineName = DEFAULT_SPEC_NAME,
    pattern: Annotated[list[str] | None, typer.Option("--pattern", "-p", help="文件名通配符，可重复")] = None,
    recursive: Annotated[bool, typer.Option("--recursive", "-r", help="递归扫描子目录")] = False,
    archive: Annotated[Path | None, typer.Option("--archive", help="处理成功后把源文件移动到该目录")] = None,
    export: Annotated[Path | None, typer.Option("--export", "-o", help="导出路径（.xlsx 或目录）")] = None,
    csv_export: Annotated[bool, typer.Option("--csv", help="导出为 CSV 目录而非 Excel")] = False,
    preview: Annotated[int, typer.Option("--preview", help="预览前 N 条记录")] = 0,
) -> None:
    """定时轮询收件目录，发现新文件就处理（Ctrl+C 停止）.

    「定时」由轮询间隔实现；生产环境也可以让系统计划任务定时调用 `docflow batch --once`。
    """
    settings = get_settings()
    settings.ensure_dirs()
    schema_def = _load_schema_or_exit(settings, schema)
    spec = _load_spec_or_exit(settings, pipeline)
    processor = BatchProcessor(schema_def, settings, spec=spec)

    console.print(f"[green]开始监听[/green] {inbox}（每 {interval:g}s 一轮，Ctrl+C 停止）{'，仅一轮' if once else ''}")
    while True:
        report = processor.process(
            inbox,
            patterns=pattern or ("*",),
            recursive=recursive,
            archive=archive,
            export=export,
            as_csv=csv_export,
        )
        if report.has_work:
            _render_batch(report, preview)
        if once:
            return
        try:
            time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - 交互式退出
            console.print("[yellow]已停止监听[/yellow]")
            return


@app.command()
def doctor() -> None:
    """环境体检：依赖、Schema、数据库、OCR 链路."""
    settings = get_settings()
    table = Table(title="docflow doctor")
    table.add_column("检查项", style="bold")
    table.add_column("状态")
    table.add_column("详情", overflow="fold")

    def add(name: str, ok: bool, detail: str) -> None:
        table.add_row(name, "[green]OK[/green]" if ok else "[red]FAIL[/red]", detail)

    for module, label in (
        ("pandas", "pandas"),
        ("openpyxl", "openpyxl"),
        ("pymupdf", "PyMuPDF"),
        ("docx", "python-docx"),
        ("rapidfuzz", "rapidfuzz"),
        ("sqlalchemy", "SQLAlchemy"),
        ("fastapi", "FastAPI"),
    ):
        try:
            __import__(module)
            add(label, True, "已安装")
        except ImportError:
            add(label, False, "未安装")

    schemas = load_schemas(settings.schemas_dir)
    add("Schema 模板", bool(schemas), f"{settings.schemas_dir} → {', '.join(sorted(schemas)) or '空'}")

    specs = load_specs(settings.pipelines_dir)
    names = sorted({DEFAULT_SPEC_NAME, *specs})
    add("Pipeline 规范", True, f"{settings.pipelines_dir} → {', '.join(names)}")

    try:
        from docflow.store import build_session_factory, review_stats

        factory = build_session_factory(settings)
        with factory() as session:
            stats = review_stats(session)
        add("数据库", True, f"{settings.resolved_database_url} | 记录分布 {stats}")
    except Exception as exc:
        add("数据库", False, str(exc))

    from docflow.ingestion.docling_backend import docling_available
    from docflow.ocr.rapid import RapidOcrEngine
    from docflow.ocr.vl import VlOcrEngine

    add("文档解析后端", settings.document_backend == "builtin" or docling_available(), settings.document_backend)
    add(
        "Docling（高级后端）",
        docling_available(),
        '未安装则 pip install "docflow-ai\\[docling]"',
    )
    add("OCR 开关", settings.ocr_enabled, f"DOCFLOW_OCR_ENABLED={settings.ocr_enabled}")
    add(
        "一级 OCR（RapidOCR）",
        RapidOcrEngine().available(),
        '未安装则 pip install "docflow-ai\\[ocr]"',
    )
    add(
        "二级 VL 模型",
        VlOcrEngine(settings).available(),
        settings.vl_base_url or "未配置 DOCFLOW_VL_BASE_URL",
    )

    from docflow.retrieval import RetrievalStore

    indexed = RetrievalStore(settings.retrieval_dir).run_ids()
    add("知识检索（RAG）", True, f"已建索引 {len(indexed)} 个 | 开关 {'开' if settings.rag_enabled else '关'}")
    add(
        "RAG 生成模型",
        settings.rag_llm_configured,
        settings.llm_base_url or "未配置 DOCFLOW_LLM_BASE_URL（问答将退化为抽取式证据）",
    )

    console.print(table)


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="监听端口")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="开发模式自动重载")] = False,
) -> None:
    """启动 Web 服务：浏览器打开 http://<host>:<port>."""
    import uvicorn

    console.print(f"[green]启动[/green] http://{host}:{port}（Ctrl+C 停止）")
    uvicorn.run("docflow.web.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":  # pragma: no cover
    app()
