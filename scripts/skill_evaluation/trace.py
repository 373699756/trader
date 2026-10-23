"""Decode Codex exec JSONL conservatively; unknown events never prove a pass."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import CommandEvidence, FinalClaim, TraceEvidence

_KNOWN_EVENTS = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "error",
    "item.started",
    "item.updated",
    "item.completed",
}
_KNOWN_ITEMS = {"command_execution", "agent_message", "reasoning", "file_change", "todo_list"}


def _claim(message: str) -> FinalClaim | None:
    try:
        raw = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    if (
        not isinstance(raw.get("summary"), str)
        or raw.get("verification") not in ("passed", "failed", "unverified", "not_run")
        or type(raw.get("live_verified")) is not bool
        or type(raw.get("completed")) is not bool
    ):
        return None
    return FinalClaim(raw["summary"], raw["verification"], raw["live_verified"], raw["completed"])


def ParseTrace(path: Path) -> TraceEvidence:
    """Recognize completed shell operations, not command text in agent messages."""
    commands: list[CommandEvidence] = []
    messages: list[str] = []
    problems: set[str] = set()
    complete = False
    input_tokens = output_tokens = None
    model = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    problems.add("invalid_jsonl")
                    continue
                if not isinstance(event, dict) or event.get("type") not in _KNOWN_EVENTS:
                    problems.add("unknown_event")
                    continue
                kind = event["type"]
                if isinstance(event.get("model"), str):
                    model = event["model"]
                if kind in ("turn.failed", "error"):
                    problems.add("agent_error")
                if kind == "turn.completed":
                    complete = True
                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = usage.get("input_tokens") if type(usage.get("input_tokens")) is int else None
                        output_tokens = usage.get("output_tokens") if type(usage.get("output_tokens")) is int else None
                if not kind.startswith("item."):
                    continue
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") not in _KNOWN_ITEMS:
                    problems.add("unknown_item")
                    continue
                if kind != "item.completed":
                    continue
                if item["type"] == "command_execution":
                    command, output, code = item.get("command"), item.get("aggregated_output"), item.get("exit_code")
                    if not isinstance(command, str) or not isinstance(output, str) or type(code) is not int:
                        problems.add("incomplete_command")
                    else:
                        commands.append(CommandEvidence(command, output, code))
                if item["type"] == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
    except (OSError, UnicodeError):
        problems.add("trace_unreadable")
    claim = _claim(messages[-1]) if messages else None
    if claim is None:
        problems.add("missing_final_claim")
    return TraceEvidence(
        tuple(commands), tuple(messages), claim, complete, tuple(sorted(problems)), model, input_tokens, output_tokens
    )
