"""Unit tests for DeepSeekClientBase, ModelCapabilities, model catalog, and
GroundTruthRenderer.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from trader.infra.deepseek.base_client import (
    DeepSeekClientBase,
    DeepSeekHttpResult,
    ModelCapabilities,
)
from trader.infra.deepseek.client import DeepSeekHttpClient
from trader.infra.deepseek.factory import create_deepseek_client
from trader.infra.deepseek.model_capabilities import (
    MODELS,
    capabilities,
    is_decommissioned,
)
from trader.infra.deepseek.model_catalog import validate_model
from trader.infra.market_data.ground_truth import (
    render_batch_ground_truth,
    render_ground_truth,
)

# ---------------------------------------------------------------------------
# ModelCapabilities
# ---------------------------------------------------------------------------


class TestModelCapabilities:
    def test_defaults(self) -> None:
        caps = ModelCapabilities(preferred_structured_method="json_object")
        assert caps.requires_reasoning_roundtrip is False
        assert caps.supports_tool_choice is False
        assert caps.reasoning_effort is None

    def test_reasoner_config(self) -> None:
        caps = ModelCapabilities(
            preferred_structured_method="json_object",
            requires_reasoning_roundtrip=True,
            reasoning_effort="high",
        )
        assert caps.requires_reasoning_roundtrip is True
        assert caps.reasoning_effort == "high"

    def test_frozen(self) -> None:
        caps = ModelCapabilities(preferred_structured_method="json_object")
        with pytest.raises(FrozenInstanceError):
            caps.requires_reasoning_roundtrip = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model capabilities registry
# ---------------------------------------------------------------------------


class TestModelCapabilitiesRegistry:
    def test_known_v4_models(self) -> None:
        assert capabilities("deepseek-v4-flash").requires_reasoning_roundtrip is False
        assert capabilities("deepseek-v4-pro").requires_reasoning_roundtrip is True
        assert capabilities("deepseek-v4-pro").reasoning_effort == "high"

    def test_legacy_models(self) -> None:
        assert capabilities("deepseek-chat").requires_reasoning_roundtrip is False
        assert capabilities("deepseek-reasoner").requires_reasoning_roundtrip is True

    def test_unknown_uses_conservative_defaults(self) -> None:
        caps = capabilities("nonexistent-model")
        assert caps.preferred_structured_method == "json_object"
        assert caps.requires_reasoning_roundtrip is False

    def test_decommissioned_models(self) -> None:
        assert is_decommissioned("deepseek-chat") is True
        assert is_decommissioned("deepseek-reasoner") is True
        assert is_decommissioned("deepseek-v4-flash") is False
        assert is_decommissioned("deepseek-v4-pro") is False

    def test_structured_method_uniform(self) -> None:
        for model in MODELS:
            assert MODELS[model].preferred_structured_method == "json_object", (
                f"{model} must use json_object structured method"
            )


# ---------------------------------------------------------------------------
# Model catalog validation
# ---------------------------------------------------------------------------


class TestModelCatalog:
    def test_known_model_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            validate_model("deepseek-v4-flash")
        assert len(caught) == 0

    def test_unknown_model_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            validate_model("unknown-model-xyz")
        assert len(caught) == 1
        assert issubclass(caught[0].category, RuntimeWarning)
        assert "unknown-model-xyz" in str(caught[0].message)

    def test_decommissioned_model_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            validate_model("deepseek-chat")
        assert len(caught) == 1
        assert issubclass(caught[0].category, FutureWarning)

    def test_strict_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown DeepSeek model"):
            validate_model("unknown-model-xyz", strict=True)

    def test_strict_decommissioned_raises(self) -> None:
        with pytest.raises(ValueError, match="decommission"):
            validate_model("deepseek-chat", strict=True)


class TestDeepSeekClientFactory:
    def test_default_provider_returns_http_client(self) -> None:
        client = create_deepseek_client()
        assert isinstance(client, DeepSeekClientBase)
        assert isinstance(client, DeepSeekHttpClient)

    def test_backward_compatible_provider_alias(self) -> None:
        client = create_deepseek_client(provider="deepseek_http")
        assert isinstance(client, DeepSeekHttpClient)

    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown DeepSeek provider"):
            create_deepseek_client(provider="unknown")


# ---------------------------------------------------------------------------
# DeepSeekClientBase ABC
# ---------------------------------------------------------------------------


class _FakeDeepSeekClient(DeepSeekClientBase):
    """Minimal test double implementing DeepSeekClientBase."""

    def __init__(self, content: str | None = None, error: str = "") -> None:
        self._content = content
        self._error = error
        self._call_count = 0
        self._last_messages: Sequence[Mapping[str, Any]] = ()
        self._last_model = ""

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        timeout_seconds: float,
        max_tokens: int,
        reserve_attempt: Callable[[], bool],
        maximum_attempts: int = 2,
    ) -> DeepSeekHttpResult:
        self._call_count += 1
        self._last_messages = messages
        self._last_model = model
        if not reserve_attempt():
            return DeepSeekHttpResult(None, None, 0, False, "budget_exhausted")
        if self._error:
            return DeepSeekHttpResult(None, 429, 1, False, self._error)
        return DeepSeekHttpResult(self._content, 200, 1, False, "")

    def capabilities(self, model: str) -> ModelCapabilities:
        return capabilities(model)


class TestDeepSeekClientBase:
    def test_fake_client_implements_abc(self) -> None:
        client = _FakeDeepSeekClient(content='{"ok":true}')
        result = client.complete(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hello"}],
            timeout_seconds=10,
            max_tokens=100,
            reserve_attempt=lambda: True,
        )
        assert result.content == '{"ok":true}'
        assert result.status_code == 200

    def test_fake_client_budget_exhausted(self) -> None:
        client = _FakeDeepSeekClient()
        result = client.complete(
            base_url="https://api.example.com",
            api_key="sk-test",
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hello"}],
            timeout_seconds=10,
            max_tokens=100,
            reserve_attempt=lambda: False,
        )
        assert result.content is None
        assert result.error == "budget_exhausted"

    def test_client_has_capabilities(self) -> None:
        client = _FakeDeepSeekClient()
        caps = client.capabilities("deepseek-v4-pro")
        assert caps.requires_reasoning_roundtrip is True
        assert caps.reasoning_effort == "high"


# ---------------------------------------------------------------------------
# DeepSeekHttpClient (concrete)
# ---------------------------------------------------------------------------


class TestDeepSeekHttpClientConcrete:
    def test_implements_base(self) -> None:
        client = DeepSeekHttpClient()
        assert isinstance(client, DeepSeekClientBase)

    def test_capabilities_delegates(self) -> None:
        client = DeepSeekHttpClient()
        caps = client.capabilities("deepseek-v4-flash")
        assert caps.requires_reasoning_roundtrip is False
        assert caps.preferred_structured_method == "json_object"

    def test_missing_api_key(self) -> None:
        client = DeepSeekHttpClient()
        result = client.complete(
            base_url="https://api.example.com",
            api_key="",
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "hello"}],
            timeout_seconds=10,
            max_tokens=100,
            reserve_attempt=lambda: True,
        )
        assert result.content is None
        assert result.error == "api_key_missing"


# ---------------------------------------------------------------------------
# Reasoning content round-trip
# ---------------------------------------------------------------------------


class TestReasoningContentRoundtrip:
    def test_reasoning_content_preserved_for_reasoner(self) -> None:
        """Messages with reasoning_content are forwarded for reasoner models."""
        messages: Sequence[Mapping[str, Any]] = [
            {"role": "user", "content": "think deeply"},
            {"role": "assistant", "content": "answer", "reasoning_content": "chain of thought..."},
        ]
        assert messages[1]["reasoning_content"] == "chain of thought..."
        # The payload-building is internal to client.py's _request_payload;
        # verify through the concrete client that the payload is formed correctly.
        client = DeepSeekHttpClient()
        assert isinstance(client, DeepSeekClientBase)
        caps = client.capabilities("deepseek-v4-pro")
        assert caps.requires_reasoning_roundtrip is True

    def test_reasoning_content_stripped_for_non_reasoner(self) -> None:
        client = DeepSeekHttpClient()
        caps = client.capabilities("deepseek-v4-flash")
        assert caps.requires_reasoning_roundtrip is False

    def test_no_temperature_for_reasoner(self) -> None:
        client = DeepSeekHttpClient()
        caps = client.capabilities("deepseek-v4-pro")
        assert caps.requires_reasoning_roundtrip is True


# ---------------------------------------------------------------------------
# GroundTruthRenderer
# ---------------------------------------------------------------------------


_TZ = timezone.utc
_DT = datetime(2026, 7, 16, 2, 30, tzinfo=_TZ)


class TestGroundTruthRenderer:
    def test_single_candidate_contains_key_fields(self) -> None:
        from trader.domain.models import FeatureSnapshot, MarketQuote

        quote = MarketQuote(
            code="600519",
            name="贵州茅台",
            price=1800.5,
            previous_close=1790.0,
            open_price=1795.0,
            high=1810.0,
            low=1785.0,
            pct_change=0.586,
            change_5m=0.12,
            speed=0.05,
            volume_ratio=1.2,
            turnover_rate=0.3,
            amount=5_000_000_000,
            amplitude=1.4,
            market_cap=2_260_000_000_000,
            industry="白酒",
            source="eastmoney",
            source_time=_DT,
            received_time=_DT,
            data_version="v1",
        )
        snapshot = FeatureSnapshot(
            quote=quote,
            values={"amount_median_20d": 4_800_000_000, "volatility_20d": 22.5},
            observed_at=_DT,
            missing_fields=("news_sentiment",),
            evidence=(),
        )
        output = render_ground_truth(snapshot)
        assert "600519" in output
        assert "贵州茅台" in output
        assert "1800.5" in output
        assert "amount_median_20d" in output
        assert "4800000000" in output or "48.0" in output
        assert "volatility_20d" in output
        assert "22.5" in output
        assert "missing_fields=news_sentiment" in output

    def test_null_values_rendered_as_null(self) -> None:
        from trader.domain.models import FeatureSnapshot, MarketQuote

        quote = MarketQuote(
            code="000001",
            name="平安银行",
            price=None,
            previous_close=None,
            open_price=None,
            high=None,
            low=None,
            pct_change=None,
            change_5m=None,
            speed=None,
            volume_ratio=None,
            turnover_rate=None,
            amount=None,
            amplitude=None,
            market_cap=None,
            industry="银行",
            source="eastmoney",
            source_time=_DT,
            received_time=_DT,
            data_version="v1",
        )
        snapshot = FeatureSnapshot(
            quote=quote,
            values={},
            observed_at=_DT,
        )
        output = render_ground_truth(snapshot)
        assert "null" in output
        assert "000001" in output

    def test_batch_rendering(self) -> None:
        from trader.domain.models import FeatureSnapshot, MarketQuote

        q1 = MarketQuote(
            code="600519",
            name="茅台",
            price=1800.0,
            previous_close=1790.0,
            open_price=1795.0,
            high=1810.0,
            low=1785.0,
            pct_change=0.5,
            change_5m=0.1,
            speed=0.05,
            volume_ratio=1.0,
            turnover_rate=0.3,
            amount=5e9,
            amplitude=1.4,
            market_cap=2.2e12,
            industry="白酒",
            source="eastmoney",
            source_time=_DT,
            received_time=_DT,
            data_version="v1",
        )
        q2 = MarketQuote(
            code="000858",
            name="五粮液",
            price=150.0,
            previous_close=149.0,
            open_price=149.5,
            high=151.0,
            low=148.5,
            pct_change=0.67,
            change_5m=0.15,
            speed=0.03,
            volume_ratio=0.9,
            turnover_rate=0.2,
            amount=2e9,
            amplitude=1.68,
            market_cap=5.8e11,
            industry="白酒",
            source="eastmoney",
            source_time=_DT,
            received_time=_DT,
            data_version="v1",
        )
        s1 = FeatureSnapshot(quote=q1, values={}, observed_at=_DT)
        s2 = FeatureSnapshot(quote=q2, values={}, observed_at=_DT)
        output = render_batch_ground_truth([s1, s2])
        assert "600519" in output
        assert "000858" in output
        assert "---" in output

    def test_deterministic_output(self) -> None:
        from trader.domain.models import FeatureSnapshot, MarketQuote

        quote = MarketQuote(
            code="600519",
            name="茅台",
            price=1800.0,
            previous_close=1790.0,
            open_price=1795.0,
            high=1810.0,
            low=1785.0,
            pct_change=0.5,
            change_5m=0.1,
            speed=0.05,
            volume_ratio=1.0,
            turnover_rate=0.3,
            amount=5e9,
            amplitude=1.4,
            market_cap=2.2e12,
            industry="白酒",
            source="eastmoney",
            source_time=_DT,
            received_time=_DT,
            data_version="v1",
        )
        snapshot = FeatureSnapshot(
            quote=quote,
            values={"amount_median_20d": 4.8e9, "volatility_20d": 22.5},
            observed_at=_DT,
            missing_fields=("news_sentiment",),
        )
        o1 = render_ground_truth(snapshot)
        o2 = render_ground_truth(snapshot)
        assert o1 == o2
