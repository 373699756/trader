"""Decision handler boundary for the read-only HTTP surface."""

from trader.http_api.handlers.product_handler import _current, _dates, _history

decision_current_handler = _current
decision_dates_handler = _dates
decision_history_handler = _history

__all__ = ["decision_current_handler", "decision_dates_handler", "decision_history_handler"]
