"""Event-stream handler boundary for the read-only HTTP surface."""

from trader.http_api.handlers.product_handler import _events as event_handler

__all__ = ["event_handler"]
