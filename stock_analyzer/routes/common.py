from __future__ import annotations

from flask import current_app, jsonify, request


def services():
    return current_app.extensions["app_services"]


def int_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def bool_arg(name: str, default: bool = False) -> bool:
    fallback = "1" if default else "0"
    return str(request.args.get(name, fallback)).lower() in ("1", "true", "yes", "on")


def json_result(result):
    payload, status = result
    return (jsonify(payload), status) if int(status or 200) != 200 else jsonify(payload)
