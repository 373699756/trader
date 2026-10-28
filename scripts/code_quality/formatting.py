"""Deterministic comma policy followed by the pinned Ruff formatter."""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path

from .files import Change, SourceFile


def _offset(text: str, row: int, column: int, *, byte_column: bool = False) -> int:
    lines = text.splitlines(keepends=True)
    prefix = sum(len(line) for line in lines[: row - 1])
    if byte_column:
        column = len(lines[row - 1].encode("utf-8")[:column].decode("utf-8"))
    return prefix + column


def _commas(text: str) -> str:
    tree = ast.parse(text)
    tokens = tuple(tokenize.generate_tokens(io.StringIO(text).readline))
    starts = tuple(_offset(text, *token.start) for token in tokens)
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.type == tokenize.OP and token.string in {"(", "[", "{"}:
            stack.append(index)
        elif token.type == tokenize.OP and token.string in {")", "]", "}"}:
            pairs[stack.pop()] = index
    edits: dict[int, tuple[int, str]] = {}
    for node in ast.walk(tree):
        opening: int | None = None
        threshold = 5
        count = 0
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            count = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
            count += int(args.vararg is not None) + int(args.kwarg is not None)
            start = _offset(text, node.lineno, node.col_offset, byte_column=True)
            opening = next(i for i in sorted(pairs) if starts[i] >= start and tokens[i].string == "(")
        elif isinstance(node, ast.Call):
            count = len(node.args) + len(node.keywords)
            assert node.func.end_lineno is not None and node.func.end_col_offset is not None
            start = _offset(text, node.func.end_lineno, node.func.end_col_offset, byte_column=True)
            opening = next(i for i in sorted(pairs) if starts[i] >= start and tokens[i].string == "(")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
                continue
            if not isinstance(node.value, (ast.List, ast.Tuple)):
                raise ValueError(f"dynamic __all__ requires review at line {node.lineno}")
            if not all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.value.elts):
                raise ValueError(f"dynamic __all__ element at line {node.lineno}")
            count, threshold = len(node.value.elts), 2
            if isinstance(node.value, ast.Tuple) and count == 1:
                continue
            start = _offset(text, node.value.lineno, node.value.col_offset, byte_column=True)
            opening = next((i for i in pairs if starts[i] == start), None)
            if opening is None:
                raise ValueError(f"unparenthesized __all__ at line {node.lineno}")
        if opening is None or not count:
            continue
        closing = pairs[opening]
        significant = [
            i
            for i in range(opening + 1, closing)
            if tokens[i].type not in {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT}
        ]
        last = significant[-1]
        has_comma = tokens[last].string == ","
        comments = any(token.type == tokenize.COMMENT for token in tokens[opening:closing])
        if count >= threshold and not has_comma:
            end = _offset(text, *tokens[last].end)
            edits[end] = (end, ",")
        elif count < threshold and has_comma and not comments:
            edits[starts[last]] = (_offset(text, *tokens[last].end), "")
    for start, (end, replacement) in sorted(edits.items(), reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _ruff(text: str, path: str, config: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--no-cache", "--config", str(config), "--stdin-filename", path, "-"],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Ruff formatting failed for {path}: {result.stderr.strip()}")
    return result.stdout


def FormatSource(source: SourceFile, config: Path) -> Change:
    formatted = _ruff(_commas(source.text), source.path, config)
    if ast.dump(ast.parse(formatted)) != ast.dump(ast.parse(source.text)):
        raise ValueError(f"format changed AST: {source.path}")
    before_comments = tuple(
        t.string for t in tokenize.generate_tokens(io.StringIO(source.text).readline) if t.type == tokenize.COMMENT
    )
    after_comments = tuple(
        t.string for t in tokenize.generate_tokens(io.StringIO(formatted).readline) if t.type == tokenize.COMMENT
    )
    if before_comments != after_comments:
        raise ValueError(f"format changed comments: {source.path}")
    if _ruff(_commas(formatted), source.path, config) != formatted:
        raise ValueError(f"format is not idempotent: {source.path}")
    if source.path.startswith("src/trader/") and len(formatted.splitlines()) > 1500:
        raise ValueError(f"formatted source exceeds 1500 lines: {source.path}")
    return Change(source, formatted)
