"""Factory for DeepSeek client implementations.

当前运行只提供 `http` 客户端，工厂层先留出 provider 扩展位，后续接入 mock/vLLM
或其他 SDK 时可不改调用方直接切换。
"""

from __future__ import annotations

from trader.infra.deepseek.base_client import DeepSeekClientBase


def create_deepseek_client(*, provider: str = "http") -> DeepSeekClientBase:
    """Create a DeepSeek client by provider.

    Args:
        provider: Logical provider name, 默认为 `http`。
            - `http`: 使用当前的 `DeepSeekHttpClient` 实现。
            - `deepseek_http`: 与 `http` 等价的兼容别名。

    Returns:
        一个实现 `DeepSeekClientBase` 的实例。
    """
    normalized = provider.strip().lower()
    if normalized in {"http", "deepseek_http"}:
        from trader.infra.deepseek.client import DeepSeekHttpClient

        return DeepSeekHttpClient()
    raise ValueError(f"Unknown DeepSeek provider '{provider}'")


__all__ = ["create_deepseek_client"]
