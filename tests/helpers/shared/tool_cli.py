"""Construct real business inputs without restoring a global production options type."""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Protocol, cast

from scripts.core.catalog import ToolByIdentity, ToolDefinition
from scripts.core.cli import ParseArguments
from scripts.core.contracts import DiagnosticCommand
from scripts.download.company.options import CompanyOptions
from scripts.download.history.options import HistoryOptions
from scripts.download.market.options import MarketOptions
from scripts.infra.database.options import DatabaseOptions
from scripts.infra.disk.options import DiskOptions
from scripts.recommendation.long.options import LongOptions
from scripts.recommendation.pipeline.options import PipelineOptions
from scripts.recommendation.web.options import WebOptions
from scripts.training.options import TrainingOptions

BusinessOptions = (
    CompanyOptions | HistoryOptions | MarketOptions | DatabaseOptions | DiskOptions
    | LongOptions | PipelineOptions | WebOptions | TrainingOptions
)
ToolArguments = tuple[ToolDefinition, argparse.Namespace]


class _BusinessParser(Protocol):
    def ParseOptions(self, arguments: argparse.Namespace) -> BusinessOptions: ...


class _BusinessCommands(Protocol):
    def BuildCommands(
        self, options: BusinessOptions, *, python_executable: str,
    ) -> tuple[DiagnosticCommand, ...]: ...


def ParseToolArguments(arguments: list[str]) -> ToolArguments:
    return ParseArguments(arguments)


def ParseBusinessOptions(arguments: ToolArguments) -> BusinessOptions:
    definition, namespace = arguments
    parser = cast(_BusinessParser, importlib.import_module(f"{definition.handler_package}.cli"))
    return parser.ParseOptions(namespace)


def BuildToolCommands(
    options: BusinessOptions, *, python_executable: str = sys.executable,
) -> tuple[DiagnosticCommand, ...]:
    definition = ToolByIdentity(options.action_identity)
    commands = cast(_BusinessCommands, importlib.import_module(f"{definition.handler_package}.commands"))
    return commands.BuildCommands(options, python_executable=python_executable)
