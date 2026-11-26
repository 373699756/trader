"""Render company field coverage for the supplied snapshot as a filterable Excel workbook."""

from __future__ import annotations

from typing import TYPE_CHECKING, BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

if TYPE_CHECKING:
    from scripts.download.company.company_field_coverage import CompanyFieldReport


def _sheet(book: Workbook, title: str, columns: tuple[tuple[str, int], ...]) -> Worksheet:
    sheet = book.create_sheet(title)
    sheet.append([name for name, _ in columns])
    for i, (_, width) in enumerate(columns, 1):
        sheet.column_dimensions[get_column_letter(i)].width = width
    return sheet


def _fraction(count: int, denominator: int) -> float | None:
    return count / denominator if denominator else None


def _style(sheet: Worksheet, *, percent: tuple[int, ...] = (), height: int = 25) -> None:
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 30
    header_fill = PatternFill("solid", fgColor="17465B")
    header_font = Font(name="Microsoft YaHei", bold=True, color="FFFFFF")
    wrapped = Alignment(vertical="center", wrap_text=True)
    for cell in sheet[1]:
        cell.fill, cell.font, cell.alignment = header_fill, header_font, wrapped
    shaded = PatternFill("solid", fgColor="EAF2F3")
    standard = Alignment(vertical="center")
    last_column = sheet.max_column
    for row in sheet.iter_rows(min_row=2):
        sheet.row_dimensions[row[0].row].height = height
        for cell in row:
            if cell.row % 2 == 0:
                cell.fill = shaded
            cell.alignment = wrapped if cell.column == last_column else standard
            if isinstance(cell.value, str):
                cell.data_type = "s"
            elif cell.column in percent:
                cell.number_format = "0.00%"
            elif isinstance(cell.value, int):
                cell.number_format = "#,##0"


def WriteCompanyFieldWorkbook(stream: BinaryIO, report: CompanyFieldReport) -> None:
    """Write the five coverage sheets to an already-open binary stream."""
    book = Workbook()
    book.remove(book["Sheet"])
    total = report.population
    guide = _sheet(book, "统计口径", (("项目", 29), ("内容", 110)))
    for row in (
        ("范围", f"{total:,} 只股票，来源轮次截止 {report.cutoff.isoformat()}。"),
        ("来源轮次", report.round_id),
        ("观察时间", report.observed_at.isoformat()),
        ("正式发布", report.publication),
        ("最新来源", "每股使用本轮最新可用报告。0 为有效数值。来源与正式覆盖页分别列出来源观测与正式核验结果。"),
        ("全池无值率", "全池无值股数除以本轮股票总数。同一字段在多条适用事件中有任一有效值，该股即计为有值。"),
        ("适用股票", "已有正式事件且字段适用的股票。有值股和已核股各自去重。有一条适用事件缺值也会计入最右列。"),
        ("覆盖与缺值", "来源有值、正式事实有值、完成核验分别统计。缺少某类事件记录仍需核对窗口覆盖。"),
        ("财务缺值", f"{len(report.missing_financial):,} 只股票，合计 {sum(len(fields) for _, fields in report.missing_financial):,} 个缺值项。未取得本轮报告的股票计入八个缺项，可与报告日期覆盖数对照。"),
        ("物理列", "SQL 非 NULL 值包括零、未知状态、空字符串及旧观测。同一股票有多行只计一次。工程表的逐股统计留空。"),
        ("比例", "分母为零时，比例留空。"),
    ):
        guide.append(row)
    _style(guide, height=38)

    main = _sheet(book, "来源与正式覆盖", (
        ("类别", 23), ("字段或指标", 32), ("有值或满足条件股数", 23), ("无值或未满足股数", 23),
        ("股票总数", 16), ("无值或未满足比例", 24), ("说明", 86),
    ))
    for metric in report.metrics:
        missing = total - metric.present
        main.append((metric.category, metric.field, metric.present, missing, total, _fraction(missing, total), metric.note))
    _style(main, percent=(6,))

    events = _sheet(book, "事件字段适用范围", (
        ("事件字段", 31), ("适用股数", 17), ("至少一项有值股数", 22), ("至少一项已核股数", 22),
        ("全池无值股数", 21), ("全池无值率", 19), ("适用但全无值股数", 25), ("适用无值率", 19),
        ("至少一条适用事件缺值股数", 32),
    ))
    for event in report.events:
        missing = total - event.present
        missing_applicable = event.applicable - event.present
        events.append((event.field, event.applicable, event.present, event.verified,
                       missing, _fraction(missing, total), missing_applicable, _fraction(missing_applicable, event.applicable),
                       event.with_missing_event))
    _style(events, percent=(6, 8))

    detail = _sheet(book, "财务缺值股票", (("股票代码", 20), ("本股缺值项数", 23), ("最新来源缺值字段", 65)))
    for code, fields in sorted(report.missing_financial):
        detail.append((code, len(fields), "、".join(fields)))
    _style(detail)

    physical = _sheet(book, "正式库全部物理列", (
        ("表", 34), ("列", 31), ("有记录股票", 20), ("有非NULL值股票", 22),
        ("全池无值股票", 20), ("全池无值率", 18), ("口径", 96),
    ))
    for column in report.columns:
        without_value = total - column.present if column.present is not None else None
        physical.append((column.table, column.field, column.stocks_with_rows, column.present, without_value,
                         _fraction(without_value, total) if without_value is not None else None, column.note))
    _style(physical, percent=(6,), height=38)
    book.save(stream)
