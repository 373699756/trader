"""Parse the current CAPCO stock-code ordered half-year industry table."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from io import BytesIO

from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError

_HEADERS = ("上市公司代码", "上市公司简称", "门类代码", "门类简称", "次类代码", "次类简称", "大类代码", "大类简称")
_MAX_BYTES = 32 * 1024 * 1024
_MAX_PAGES = 300
_MAX_ROWS = 20_000
_MAX_PAGE_CHARACTERS = 100_000


@dataclass(frozen=True)
class OfficialIndustryRow:
    code: str
    company_name: str
    native_category: str
    native_name: str
    page: int
    section_code: str = ""
    section_name: str = ""
    subcategory_code: str = ""
    subcategory_name: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str) or re.fullmatch(r"[0-9]{6}", self.code) is None
            or not isinstance(self.company_name, str) or not 1 <= len(self.company_name) <= 40
            or self.company_name != self.company_name.strip()
            or not isinstance(self.native_category, str) or re.fullmatch(r"[A-S][0-9]{2}", self.native_category) is None
            or not isinstance(self.native_name, str) or not 1 <= len(self.native_name) <= 100
            or self.native_name != self.native_name.strip()
            or type(self.page) is not int or not 1 <= self.page <= _MAX_PAGES
            or not isinstance(self.section_code, str) or not isinstance(self.section_name, str)
            or not isinstance(self.subcategory_code, str) or not isinstance(self.subcategory_name, str)
        ):
            raise ValueError("official_industry_pdf_row_invalid")
        if (
            bool(self.section_code) != bool(self.section_name)
            or self.section_code and self.section_code != self.native_category[0]
            or len(self.section_name) > 100
            or bool(self.subcategory_code) != bool(self.subcategory_name)
            or self.subcategory_code and (
                not self.section_code or re.fullmatch(self.section_code + r"[A-Z]", self.subcategory_code) is None
            )
            or len(self.subcategory_name) > 100
        ):
            raise ValueError("official_industry_pdf_row_invalid")


@dataclass(frozen=True)
class OfficialIndustrySnapshot:
    rows: tuple[OfficialIndustryRow, ...]
    page_count: int

    def __post_init__(self) -> None:
        if (
            type(self.rows) is not tuple or not 1 <= len(self.rows) <= _MAX_ROWS
            or any(not isinstance(row, OfficialIndustryRow) for row in self.rows)
            or type(self.page_count) is not int or not 1 <= self.page_count <= _MAX_PAGES
            or any(row.page > self.page_count for row in self.rows)
            or tuple(row.code for row in self.rows) != tuple(sorted({row.code for row in self.rows}))
        ):
            raise ValueError("official_industry_pdf_snapshot_invalid")


@dataclass(frozen=True)
class _TextRun:
    text: str
    x: float
    y: float


def _check_cancel(cancel_requested: Callable[[], bool]) -> None:
    if cancel_requested():
        raise RuntimeError("supplier_cancelled")


def _coordinate(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("official_industry_pdf_coordinate_invalid")
    return float(value)


def _point(x: float, y: float, matrix: list[float]) -> tuple[float, float]:
    return x * matrix[0] + y * matrix[2] + matrix[4], x * matrix[1] + y * matrix[3] + matrix[5]


def _grid_lines(values: list[float]) -> tuple[float, ...]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and value - clusters[-1][0] <= 0.75:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return tuple(sum(cluster) / len(cluster) for cluster in clusters)


def _parse_row(cells: tuple[str, ...], page: int) -> OfficialIndustryRow:
    code, company, section, section_name, subcategory, subcategory_name, category, category_name = cells
    if (
        re.fullmatch(r"[0-9]{6}", code) is None
        or not 1 <= len(company) <= 40
        or re.fullmatch(r"[A-S]", section) is None
        or not 1 <= len(section_name) <= 100
        or re.fullmatch(r"[0-9]{2}", category) is None
        or not 1 <= len(category_name) <= 100
        or bool(subcategory) != bool(subcategory_name)
        or (subcategory and re.fullmatch(section + r"[A-Z]", subcategory) is None)
        or len(subcategory_name) > 100
    ):
        raise ValueError("official_industry_pdf_row_invalid")
    return OfficialIndustryRow(
        code,
        company,
        section + category,
        category_name,
        page,
        section,
        section_name,
        subcategory,
        subcategory_name,
    )


def _page_rows(
    runs: list[_TextRun],
    horizontal: list[float],
    vertical: list[float],
    title: str,
    page_number: int,
) -> tuple[OfficialIndustryRow, ...]:
    columns = _grid_lines(vertical)
    borders = _grid_lines(horizontal)
    if len(columns) != 9 or len(borders) < 2:
        raise ValueError("official_industry_pdf_layout_unsupported")
    if page_number == 1 and not any(re.sub(r"\s+", "", run.text) == title for run in runs):
        raise ValueError("official_industry_pdf_period_mismatch")
    cells: dict[tuple[int, int], list[_TextRun]] = {}
    for run in runs:
        compact = re.sub(r"\s+", "", run.text)
        if compact == title:
            continue
        column = bisect_right(columns, run.x) - 1
        row = bisect_right(borders, run.y) - 1
        if not 0 <= column < 8 or not 0 <= row < len(borders) - 1:
            raise ValueError("official_industry_pdf_text_outside_table")
        cells.setdefault((row, column), []).append(run)
    table: list[tuple[str, ...]] = []
    for row in range(len(borders) - 2, -1, -1):
        values = tuple(
            "".join(re.sub(r"\s+", "", run.text) for run in sorted(cells.get((row, column), ()), key=lambda item: (-item.y, item.x)))
            for column in range(8)
        )
        if any(values):
            table.append(values)
    if not table or table[0] != _HEADERS:
        raise ValueError("official_industry_pdf_layout_unsupported")
    if len(table) == 1:
        raise ValueError("official_industry_pdf_empty_page")
    return tuple(_parse_row(values, page_number) for values in table[1:])


def _read_page(
    page: PageObject, cancel_requested: Callable[[], bool]
) -> tuple[list[_TextRun], list[float], list[float]]:
    runs: list[_TextRun] = []
    vertical: list[float] = []
    horizontal: list[float] = []
    previous: tuple[float, float] | None = None

    def visit_text(text: str, matrix: list[float], text_matrix: list[float], font: object, font_size: float) -> None:
        _check_cancel(cancel_requested)
        if text.strip():
            x, y = _point(text_matrix[4], text_matrix[5], matrix)
            runs.append(_TextRun(text, x, y))

    def visit_operand(operator: bytes, operands: list[object], matrix: list[float], text_matrix: list[float]) -> None:
        nonlocal previous
        _check_cancel(cancel_requested)
        if operator not in (b"m", b"l"):
            return
        if len(operands) != 2:
            raise ValueError("official_industry_pdf_grid_invalid")
        current = _point(_coordinate(operands[0]), _coordinate(operands[1]), matrix)
        if operator == b"l" and previous is not None:
            if abs(previous[0] - current[0]) < 0.05 and abs(previous[1] - current[1]) >= 10:
                vertical.append(current[0])
            if abs(previous[1] - current[1]) < 0.05 and abs(previous[0] - current[0]) >= 10:
                horizontal.append(current[1])
        previous = current

    text = page.extract_text(visitor_text=visit_text, visitor_operand_before=visit_operand)
    if not text or len(text) > _MAX_PAGE_CHARACTERS:
        raise ValueError("official_industry_pdf_page_invalid")
    return runs, horizontal, vertical


def ParseOfficialIndustryPdf(
    content: bytes,
    period_end: date,
    *,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> OfficialIndustrySnapshot:
    """Keep the current native hierarchy; never reinterpret earlier classifications."""
    if type(period_end) is not date or (period_end.month, period_end.day) not in ((6, 30), (12, 31)):
        raise ValueError("official_industry_period_invalid")
    if not content.startswith(b"%PDF-") or len(content) > _MAX_BYTES:
        raise ValueError("official_industry_pdf_invalid")
    title = f"{period_end.year}年{'上' if period_end.month == 6 else '下'}半年上市公司行业分类结果"
    rows: list[OfficialIndustryRow] = []
    seen_codes: set[str] = set()
    _check_cancel(cancel_requested)
    try:
        with BytesIO(content) as stream, PdfReader(stream) as reader:
            if reader.is_encrypted or not 1 <= len(reader.pages) <= _MAX_PAGES:
                raise ValueError("official_industry_pdf_pages_invalid")
            page_count = len(reader.pages)
            for page_number, page in enumerate(reader.pages, 1):
                _check_cancel(cancel_requested)
                runs, horizontal, vertical = _read_page(page, cancel_requested)
                for row in _page_rows(runs, horizontal, vertical, title, page_number):
                    if row.code in seen_codes:
                        raise ValueError("official_industry_pdf_duplicate_code")
                    if rows and row.code < rows[-1].code:
                        raise ValueError("official_industry_pdf_code_order_invalid")
                    seen_codes.add(row.code)
                    rows.append(row)
                    if len(rows) > _MAX_ROWS:
                        raise ValueError("official_industry_pdf_rows_limit")
            _check_cancel(cancel_requested)
    except PdfReadError as exc:
        raise ValueError("official_industry_pdf_invalid") from exc
    return OfficialIndustrySnapshot(tuple(rows), page_count)


__all__ = [
    "OfficialIndustryRow",
    "OfficialIndustrySnapshot",
    "ParseOfficialIndustryPdf",
]
