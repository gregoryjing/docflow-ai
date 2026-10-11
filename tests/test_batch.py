"""批处理与增量去重：收集、指纹、账本、归档、导出（全部用真实管线，不依赖外部服务）."""

from __future__ import annotations

from pathlib import Path

from docflow.batch import BatchProcessor, ProcessedLedger, collect_files, file_fingerprint

ORDERS = (
    "订单编号,供应商,物料编号,物料名称,数量,单价,总金额,下单日期,交付日期,是否加急\n"
    "PO-2024-001,华信机械,A001,螺栓,100,0.5,50,2024-03-01,2024-03-15,否\n"
    "PO-2024-002,远大五金,B002,齿轮,20,12.5,250,2024-03-02,2024-03-20,否\n"
)


def _write_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "orders_a.csv").write_text(ORDERS, encoding="utf-8")
    (inbox / "orders_b.csv").write_text(ORDERS.replace("PO-2024-002", "PO-2024-003"), encoding="utf-8")
    (inbox / "notes.txt").write_text("不是表格", encoding="utf-8")
    return inbox


def test_collect_files_respects_pattern_and_recursion(tmp_path: Path):
    inbox = _write_inbox(tmp_path)
    nested = inbox / "sub"
    nested.mkdir()
    (nested / "orders_c.csv").write_text(ORDERS, encoding="utf-8")

    flat = collect_files(inbox, patterns=("*.csv",))
    assert [path.name for path in flat] == ["orders_a.csv", "orders_b.csv"]

    deep = collect_files(inbox, patterns=("*.csv",), recursive=True)
    assert [path.name for path in deep] == ["orders_a.csv", "orders_b.csv", "orders_c.csv"]


def test_file_fingerprint_is_content_based(tmp_path: Path):
    first = tmp_path / "a.csv"
    second = tmp_path / "renamed.csv"
    first.write_text(ORDERS, encoding="utf-8")
    second.write_text(ORDERS, encoding="utf-8")
    assert file_fingerprint(first) == file_fingerprint(second)


def test_ledger_round_trips_to_disk(tmp_path: Path):
    path = tmp_path / "state" / "processed.json"
    ledger = ProcessedLedger(path)
    assert not ledger.seen("abc")
    ledger.record("abc", "orders.csv", "run-1")
    ledger.save()

    reloaded = ProcessedLedger(path)
    assert reloaded.seen("abc")
    assert len(reloaded) == 1


def test_ledger_survives_corrupt_file(tmp_path: Path):
    path = tmp_path / "processed.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert len(ProcessedLedger(path)) == 0


def test_batch_processes_then_skips_processed_files(settings, purchase_order, tmp_path: Path):
    inbox = _write_inbox(tmp_path)
    processor = BatchProcessor(purchase_order, settings)

    first = processor.process(inbox, patterns=("*.csv",))
    assert sorted(path.name for path in first.processed) == ["orders_a.csv", "orders_b.csv"]
    assert not first.skipped
    assert first.result is not None
    assert first.result.total_records == 4

    second = processor.process(inbox, patterns=("*.csv",))
    assert not second.processed
    assert sorted(path.name for path in second.skipped) == ["orders_a.csv", "orders_b.csv"]
    assert second.result is None  # 没有新文件就不跑管线


def test_batch_dedupes_identical_files_within_one_round(settings, purchase_order, tmp_path: Path):
    """同一轮里两份字节相同的文件（改名副本）只处理一次，另一份按「已处理」跳过."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "orders.csv").write_text(ORDERS, encoding="utf-8")
    (inbox / "orders_renamed.csv").write_text(ORDERS, encoding="utf-8")

    report = BatchProcessor(purchase_order, settings).process(inbox, patterns=("*.csv",))

    assert [path.name for path in report.processed] == ["orders.csv"]
    assert [path.name for path in report.skipped] == ["orders_renamed.csv"]
    assert report.result is not None
    assert report.result.total_records == 2  # 只入库一份，不重复


def test_batch_reprocess_bypasses_ledger(settings, purchase_order, tmp_path: Path):
    inbox = _write_inbox(tmp_path)
    processor = BatchProcessor(purchase_order, settings)
    processor.process(inbox, patterns=("*.csv",))

    again = processor.process(inbox, patterns=("*.csv",), reprocess=True)
    assert sorted(path.name for path in again.processed) == ["orders_a.csv", "orders_b.csv"]


def test_batch_archives_successful_files(settings, purchase_order, tmp_path: Path):
    inbox = _write_inbox(tmp_path)
    archive = tmp_path / "archive"
    processor = BatchProcessor(purchase_order, settings)

    report = processor.process(inbox, patterns=("*.csv",), archive=archive)

    assert report.result is not None
    assert not (inbox / "orders_a.csv").exists()
    assert (archive / "orders_a.csv").exists()
    assert (inbox / "notes.txt").exists()  # 非目标文件不动


def test_batch_exports_results(settings, purchase_order, tmp_path: Path):
    inbox = _write_inbox(tmp_path)
    export_dir = tmp_path / "out"
    processor = BatchProcessor(purchase_order, settings)

    report = processor.process(inbox, patterns=("*.csv",), export=export_dir)

    assert report.export_path is not None
    assert report.export_path.is_dir()
    assert (report.export_path / "records.csv").exists()
    assert (report.export_path / "summary.csv").exists()


def test_batch_empty_inbox_is_noop(settings, purchase_order, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = BatchProcessor(purchase_order, settings).process(empty)
    assert not report.has_work
    assert report.result is None


def test_batch_records_failures_without_blocking(settings, purchase_order, tmp_path: Path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "good.csv").write_text(ORDERS, encoding="utf-8")
    # .xlsx 扩展名但内容不是表格 → 解析失败，应留在原地待重试
    (inbox / "broken.xlsx").write_text("这不是 Excel", encoding="utf-8")

    report = BatchProcessor(purchase_order, settings).process(inbox)

    assert [path.name for path in report.processed] == ["good.csv"]
    assert [path.name for path in report.failed] == ["broken.xlsx"]
    assert (inbox / "broken.xlsx").exists()
    assert report.result is not None
