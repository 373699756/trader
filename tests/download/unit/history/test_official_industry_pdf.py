from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from trader.download.infra import official_industry_pdf as pdf_module
from trader.download.infra.official_industry_pdf import ParseOfficialIndustryPdf

_PERIOD = date(2026, 6, 30)
_TITLE = "2026年上半年上市公司行业分类结果"
_HEADERS = ("上市公司代码", "上市公司简称", "门类代码", "门类简称", "次类代码", "次类简称", "大类代码", "大类简称")
_BANK = ("000001", "平安银行", "J", "金融业", "", "", "66", "货币金融服务")
_MANUFACTURER = ("000008", "神州高铁", "C", "制造业", "CG", "专用、通用及交通运输设备", "37", "铁路、船舶、航空航天和其他\n运输设备制造业")
_MATRIX = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


class _Page:
    def __init__(
        self,
        rows: tuple[tuple[str, ...], ...],
        *,
        title: str = "",
        headers: tuple[str, ...] = _HEADERS,
        extra: tuple[str, float, float] | None = None,
    ) -> None:
        self._rows = rows
        self._title = title
        self._headers = headers
        self._extra = extra

    def extract_text(
        self,
        *,
        visitor_text: Callable[[str, list[float], list[float], object, float], None],
        visitor_operand_before: Callable[[bytes, list[object], list[float], list[float]], None],
    ) -> str:
        columns = (30, 70, 110, 150, 250, 290, 410, 450, 560)
        borders = tuple(765 - 30 * row for row in range(len(self._rows) + 2))
        for x in columns:
            visitor_operand_before(b"m", [x, borders[0]], _MATRIX, _MATRIX)
            visitor_operand_before(b"l", [x, borders[-1]], _MATRIX, _MATRIX)
        for y in borders:
            visitor_operand_before(b"m", [columns[0], y], _MATRIX, _MATRIX)
            visitor_operand_before(b"l", [columns[-1], y], _MATRIX, _MATRIX)

        def text(value: str, x: float, y: float) -> None:
            visitor_text(value, _MATRIX, [1, 0, 0, 1, x, y], None, 8.0)

        if self._title:
            text(self._title, 200, 790)
        for column, header in enumerate(self._headers):
            if column < 2:
                text(header[:4], columns[column] + 2, 755)
                text(header[4:], columns[column] + 2, 745)
            else:
                text(header, columns[column] + 2, 750)
        for row_number, row in enumerate(self._rows):
            for column, value in enumerate(row):
                parts = value.split("\n")
                for offset, part in enumerate(parts):
                    text(part, columns[column] + 2, 720 - 30 * row_number + (len(parts) - 1) * 5 - offset * 10)
        if self._extra is not None:
            text(*self._extra)
        return self._title + "".join(self._headers) + "".join("".join(row) for row in self._rows)


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    configured: tuple[_Page, ...] = ()

    class _Reader:
        is_encrypted = False

        def __init__(self, stream: object) -> None:
            self.pages = configured

        def __enter__(self) -> _Reader:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def configure(*values: _Page) -> None:
        nonlocal configured
        configured = values

    monkeypatch.setattr(pdf_module, "PdfReader", _Reader)
    return configure


def test_current_capco_hierarchy_and_wrapped_cells_are_retained(pages: Callable[..., None]) -> None:
    pages(_Page((_BANK, _MANUFACTURER), title=_TITLE))
    result = ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)
    assert result.page_count == 1
    assert [(row.code, row.native_category) for row in result.rows] == [("000001", "J66"), ("000008", "C37")]
    assert result.rows[0].subcategory_code == ""
    assert result.rows[1].section_code == "C"
    assert result.rows[1].section_name == "制造业"
    assert result.rows[1].subcategory_code == "CG"
    assert result.rows[1].subcategory_name == "专用、通用及交通运输设备"
    assert result.rows[1].native_name == "铁路、船舶、航空航天和其他运输设备制造业"


def test_each_page_requires_current_header_and_preserves_page_number(pages: Callable[..., None]) -> None:
    pages(_Page((_BANK,), title=_TITLE), _Page((_MANUFACTURER,)))
    result = ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)
    assert [row.page for row in result.rows] == [1, 2]


@pytest.mark.parametrize(
    "row",
    (
        ("00001", *_BANK[1:]),
        ("", *_BANK[1:]),
        (_BANK[0], "", *_BANK[2:]),
        (*_BANK[:2], "金融", *_BANK[3:]),
        (*_BANK[:4], "CG", "", *_BANK[6:]),
        (*_BANK[:4], "CG", "错误门类", *_BANK[6:]),
        (*_BANK[:6], "C66", _BANK[7]),
    ),
)
def test_malformed_or_incomplete_native_rows_are_rejected(pages: Callable[..., None], row: tuple[str, ...]) -> None:
    pages(_Page((row,), title=_TITLE))
    with pytest.raises(ValueError, match="row_invalid"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


def test_unparsed_text_outside_table_is_not_ignored(pages: Callable[..., None]) -> None:
    pages(_Page((_BANK,), title=_TITLE, extra=("600000浦发银行", 10, 720)))
    with pytest.raises(ValueError, match="text_outside_table"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


@pytest.mark.parametrize("rows,reason", (((_BANK, _BANK), "duplicate_code"), ((_MANUFACTURER, _BANK), "code_order_invalid")))
def test_duplicate_or_out_of_order_stock_codes_are_rejected(pages: Callable[..., None], rows: tuple[tuple[str, ...], ...], reason: str) -> None:
    pages(_Page(rows, title=_TITLE))
    with pytest.raises(ValueError, match=reason):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


@pytest.mark.parametrize("title", ("2025年下半年上市公司行业分类结果", "2018年3季度上市公司行业分类结果"))
def test_wrong_period_and_old_quarterly_tables_are_rejected(pages: Callable[..., None], title: str) -> None:
    pages(_Page((_BANK,), title=title))
    with pytest.raises(ValueError, match="period_mismatch|text_outside_table"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


def test_earlier_five_column_header_is_rejected(pages: Callable[..., None]) -> None:
    pages(_Page((_BANK,), title=_TITLE, headers=("门类名称及代码", "行业大类代码", "行业大类名称", "上市公司代码", "上市公司简称")))
    with pytest.raises(ValueError, match="layout_unsupported"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


@pytest.mark.parametrize("period", (date(2026, 3, 31), date(2026, 6, 29)))
def test_only_half_year_period_ends_are_accepted(period: date) -> None:
    with pytest.raises(ValueError, match="period_invalid"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", period)


def test_empty_table_is_rejected(pages: Callable[..., None]) -> None:
    pages(_Page((), title=_TITLE))
    with pytest.raises(ValueError, match="empty_page"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)


def test_cancellation_propagates_from_pdf_operator_visit(pages: Callable[..., None]) -> None:
    pages(_Page((_BANK,), title=_TITLE))
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls == 4

    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD, cancel_requested=cancelled)


def test_pdf_byte_and_row_limits(pages: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="pdf_invalid"):
        ParseOfficialIndustryPdf(b"html", _PERIOD)
    pages(_Page((_BANK, _MANUFACTURER), title=_TITLE))
    monkeypatch.setattr(pdf_module, "_MAX_ROWS", 1)
    with pytest.raises(ValueError, match="rows_limit"):
        ParseOfficialIndustryPdf(b"%PDF-fixture", _PERIOD)
    monkeypatch.setattr(pdf_module, "_MAX_BYTES", 5)
    with pytest.raises(ValueError, match="pdf_invalid"):
        ParseOfficialIndustryPdf(b"%PDF-too-large", _PERIOD)
