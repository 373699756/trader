"""Status handler boundary for the read-only HTTP surface."""

from trader.http_api.handlers.product_handler import _status as status_handler

__all__ = ["status_handler"]
