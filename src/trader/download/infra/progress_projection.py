"""Download facts projected to human-readable messages at the output adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date

from trader.infra import progress_format
from trader.infra.progress_format import CleanProgressText, DisplayWidth, ProgressDuration
from trader.download.application.progress_message import (
    ProgressField, ProgressLevel, ProgressMessage, ProgressRow, ProgressState, ProgressTask,
)
from trader.download.application.progress_events import (
    DownloadProgressEvent, ProgressCount, ProgressDate, ProgressDetail, ProgressMeasure,
    ProgressRatio, QfqNotice, QfqPendingSummary,
)
from trader.download.application.stock_download_progress import StockDownloadProgress

_STOCK_NAME_WIDTH = 8


def FormatProgressField(text: str, width: int = 0, *, level: ProgressLevel = ProgressLevel.NORMAL) -> ProgressField:
    value = progress_format.CleanProgressText(text)
    return ProgressField(value + " " * max(0, width - progress_format.DisplayWidth(value)), level)


def ProgressNumber(value: int | None, width: int = 6) -> str:
    return ("-" if value is None else str(value)).rjust(width)


def WithProgressTime(message: ProgressMessage, total_seconds: float, stage_seconds: float) -> ProgressMessage:
    if message.task is None:
        raise ValueError("sync_progress_task_missing")
    prefix = progress_format.ProgressPrefix(
        total_seconds, f"{message.task.owner}·{message.task.label}", stage_seconds, message.state,
        stage_width=30 if message.compact else 52,
    )
    rows: list[ProgressRow] = []
    for row in message.rows:
        row_prefix = progress_format.ProgressPrefix(
            total_seconds, f"{message.task.owner}·{row.label}", stage_seconds, message.state,
            stage_width=30 if message.compact else 52,
        )
        rows.append(replace(row, fields=(
            *(ProgressField(part) for part in (row_prefix[:2] if message.compact else row_prefix)),
            *row.fields,
        )))
    return replace(message, fields=(
        *(ProgressField(part) for part in (prefix[:2] if message.compact else prefix)),
        *message.fields,
    ), rows=tuple(rows))


def FormatStockDownloadProgress(progress: StockDownloadProgress) -> ProgressMessage:
    percent = progress.completed / progress.total * 100 if progress.total else 100.0
    name = CleanProgressText(progress.name or "—").strip() or "—"
    short_name = name
    if DisplayWidth(name) > _STOCK_NAME_WIDTH:
        short_name = ""
        for char in name:
            if DisplayWidth(short_name + char) > _STOCK_NAME_WIDTH - 1:
                break
            short_name += char
        short_name += "…"
    level = _result_level(progress)
    missing = "未知" if progress.missing is None else str(progress.missing)
    parts = [
        ProgressField(f"{'已确认' if progress.command == 'sync history' else '已处理'} {progress.completed}/{progress.total}（{percent:.2f}%）"),
        FormatProgressField(
            f"股票 {progress.code} {short_name}", width=DisplayWidth(f"股票 {progress.code} ") + _STOCK_NAME_WIDTH
        ),
        FormatProgressField(f"处理 {progress.action}"),
        FormatProgressField(f"结果 {progress.result}", level=level),
        FormatProgressField(f"raw {_format_count(progress.raw, progress.target)}"),
        FormatProgressField(f"qfq {_format_count(progress.qfq, progress.target)}"),
        FormatProgressField(f"完整配对 {_format_count(progress.paired, progress.target)}"),
        FormatProgressField(
            f"缺日 {missing}", level=ProgressLevel.WARNING if progress.missing != 0 else ProgressLevel.NORMAL
        ),
        *((FormatProgressField(f"推定停牌 {progress.inferred_suspended}"),) if progress.inferred_suspended else ()),
        *(FormatProgressField(item) for item in progress.extra),
        *((FormatProgressField(f"原来源 {_source_label(progress.source)}"),) if progress.source is not None else ()),
        FormatProgressField(
            f"原因 {progress.reason}", level=level if progress.reason != "无" else ProgressLevel.NORMAL
        ),
    ]
    if progress.missing_dates:
        parts.append(FormatProgressField(f"缺失日期 {_format_missing_dates(progress.missing_dates)}"))
    if short_name != name:
        parts.append(FormatProgressField(f"完整名称 {name}"))
    if progress.target is None:
        parts.append(ProgressField("本次覆盖 未重新核验"))
    return ProgressMessage(
        tuple(parts), progress.reason if progress.result in {"失败", "待重试"} else None,
        task=ProgressTask("history" if progress.command == "sync history" else "market",
                          "prices" if progress.command == "sync history" else "价格更新",
                          "Tencent下载" if progress.command == "sync history" else progress.action),
        state="失败" if progress.result == "失败" else "重试" if progress.result == "待重试" else "处理中",
    )


def _result_level(progress: StockDownloadProgress) -> ProgressLevel:
    if progress.result == "失败":
        return ProgressLevel.ERROR
    if progress.result == "待重试":
        return progress.failure_level
    if progress.result == "复用":
        return ProgressLevel.NORMAL
    if progress.result == "部分" or progress.missing != 0:
        return ProgressLevel.WARNING
    if progress.result == "合格":
        return ProgressLevel.SUCCESS
    return ProgressLevel.NORMAL


def _format_count(value: int | None, target: int | None) -> str:
    return f"{'-' if value is None else value}/{'-' if target is None else target}"


def _format_missing_dates(days: tuple[date, ...]) -> str:
    preview = ", ".join(day.isoformat() for day in days[:10])
    if len(days) > 10:
        preview += f"，另有 {len(days) - 10} 日（共 {len(days)} 日）"
    return preview


def _source_label(source: str) -> str:
    provider = source.partition(":")[0]
    return {"tencent-newfqkline": "Tencent", "baostock": "BaoStock"}.get(provider, provider)


def _detail(value: ProgressDetail) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, ProgressCount):
        unit = f" {value.unit}" if value.unit in {"股", "只", "股日"} else value.unit
        return f"{value.label + ' ' if value.label else ''}{value.value}{unit}"
    if isinstance(value, ProgressRatio):
        return f"{value.label} {value.completed}/{value.total}"
    if isinstance(value, ProgressDate):
        return f"{value.label} {value.value.isoformat()}"
    if isinstance(value, ProgressMeasure):
        return f"{value.label} {value.value}{value.unit}"
    return f"来源 {_source_label(value.value)}"


def QfqMessage(
    stage: str, details: tuple[ProgressDetail, ...], *, level: ProgressLevel = ProgressLevel.NORMAL,
    state: ProgressState = "处理中", finished: bool = False, label: str | None = None,
    source: str | None = None,
) -> ProgressMessage:
    return ProgressMessage(
        tuple(
            FormatProgressField(_detail(part), level=level if level is ProgressLevel.ERROR else ProgressLevel.NORMAL)
            for part in details
        ),
        task=ProgressTask("market", stage, label or (f"{_source_label(source)}下载" if source is not None else stage)),
        state="失败" if level is ProgressLevel.ERROR else state,
        finished=finished or level is ProgressLevel.ERROR,
    )


def ProjectDownloadProgress(event: DownloadProgressEvent) -> ProgressMessage:
    if isinstance(event, ProgressMessage):
        return event
    if isinstance(event, StockDownloadProgress):
        return FormatStockDownloadProgress(event)
    if isinstance(event, QfqNotice):
        return QfqMessage(
            event.stage, event.details, level=event.level, state=event.state, finished=event.finished,
            label=event.label, source=event.source,
        )
    if isinstance(event, QfqPendingSummary):
        return QfqMessage(
            "待重试原因",
            tuple(f"{item.reason} {ProgressNumber(item.count)} 股" for item in event.reasons),
            level=event.level,
        )
    percent = event.processed / event.total * 100 if event.total else 100.0
    return QfqMessage(
        event.stage,
        (
            f"已处理 {ProgressNumber(event.processed)}/{ProgressNumber(event.total)}（{percent:6.2f}%） ",
            f"合格 {ProgressNumber(event.completed)}", f"待重试 {ProgressNumber(event.pending)}",
            f"其中复用 {ProgressNumber(event.skipped)}", f"无需下载 {ProgressNumber(event.not_applicable)}",
            f"无真实日线 {ProgressNumber(event.no_trade_data)}", f"最近日停牌 {ProgressNumber(event.latest_suspended)}",
            f"最近日推定停牌 {ProgressNumber(event.latest_inferred_suspended)}",
            f"最近日未解释缺价 {ProgressNumber(event.latest_missing)}",
            f"补采待核 {ProgressNumber(event.unresolved_supplement_codes)}",
            f"未处理 {ProgressNumber(event.total - event.processed)}",
            f"变更文件 {ProgressNumber(event.changed_files)}", f"变更行 {ProgressNumber(event.rows)}",
            f"落库耗时 {ProgressDuration(event.write_seconds)}",
        ),
        level=event.level, state="完成", finished=True,
    )


class ProjectingDownloadProgress:
    """Join an injected fact port to an existing message-only output consumer."""

    def __init__(self, report: Callable[[ProgressMessage], None]) -> None:
        self._report = report

    def __call__(self, event: DownloadProgressEvent) -> None:
        self._report(ProjectDownloadProgress(event))


__all__ = [
    "FormatProgressField", "FormatStockDownloadProgress", "ProgressNumber", "WithProgressTime",
    "QfqMessage", "ProjectDownloadProgress", "ProjectingDownloadProgress",
]
