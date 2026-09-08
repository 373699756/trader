"""Shared BaoStock archive errors without adapter import cycles."""


class BaoStockDailyArtifactConflictError(RuntimeError):
    """Raised when a shard, catalog, or manifest changes identity."""


__all__ = ["BaoStockDailyArtifactConflictError"]
