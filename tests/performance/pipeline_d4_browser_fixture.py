from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Flask, jsonify

from tests.performance.test_pipeline_d4_web import _Archive, _snapshot
from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.application.publisher import SnapshotPublisher, encode_sse
from trader.application.queries import RecommendationQueries
from trader.domain.market.models import FeatureSnapshot, LiveQuote, MarketQuote
from trader.domain.recommendation.models import LiveOverlay
from trader.web import create_app

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _feature(code: str, observed_at: datetime, *, industry: str) -> FeatureSnapshot:
    quote = MarketQuote(
        code=code,
        name=f"桌面验收{code}",
        price=12.0,
        previous_close=11.65,
        open_price=11.8,
        high=12.2,
        low=11.7,
        pct_change=3.0,
        change_5m=1.0,
        speed=0.8,
        volume_ratio=2.0,
        turnover_rate=3.0,
        amount=300_000_000.0,
        amplitude=4.0,
        market_cap=30_000_000_000.0,
        industry=industry,
        source="d4-offline-fixture",
        source_time=observed_at,
        received_time=observed_at,
        data_version=f"fixture:{code}",
    )
    return FeatureSnapshot(quote=quote, values={}, observed_at=observed_at, history_days=60)


def build_app() -> Flask:
    observed_at = datetime.now(SHANGHAI).replace(hour=10, minute=0, second=0, microsecond=0)
    archive = _Archive()
    index = PublishedSnapshotIndex(archive)
    publisher = SnapshotPublisher(history_size=64, client_queue_size=8)
    initial = replace(
        _snapshot("d4-browser-000", _feature),
        trade_date=observed_at.date().isoformat(),
        published_at=observed_at,
    )
    index.publish(initial)
    publisher.publish(initial)
    state = {"snapshot_id": initial.snapshot_id, "tick": 0}
    lock = Lock()

    def status() -> dict[str, object]:
        return {
            "schema_version": "v3",
            "status": "running",
            "runtime_started": True,
            "phase": "today_main",
            "last_error": (
                "TopK live overlay degraded: data_source_task exceeded its bounded deadline; "
                "the last valid projection remains visible while the source lane recovers"
            ),
            "dependencies": {
                "deepseek": {"budget": {"available": True, "used": 17, "remaining": 171}},
                "market_data": {
                    "active_source": "eastmoney+sina",
                    "route": {
                        "status": "success",
                        "degraded": True,
                        "fallback_reason": "offline_d4_fixture",
                        "used_vendor": "fixture",
                        "attempted_vendors": [],
                    },
                },
            },
            "strategies": {"today": {"snapshot_id": state["snapshot_id"]}},
        }

    queries = RecommendationQueries(index, now=lambda: observed_at)
    app = create_app(status, queries=queries, publisher=publisher)

    @app.post("/__d4/publish")
    def publish_fixture_update():
        with lock:
            state["tick"] += 1
            tick = state["tick"]
            snapshot = replace(
                _snapshot(
                    f"d4-browser-{tick:03d}",
                    _feature,
                    changed_price=12.0 + tick / 100,
                ),
                trade_date=observed_at.date().isoformat(),
                published_at=observed_at,
            )
            index.publish(snapshot)
            event = publisher.publish(snapshot)
            assert event is not None
            state["snapshot_id"] = snapshot.snapshot_id
        return jsonify(
            {
                "snapshot_id": snapshot.snapshot_id,
                "sequence": event.sequence,
                "sse_bytes": len(encode_sse(event).encode("utf-8")),
            }
        )

    @app.post("/__d4/resync")
    def publish_fixture_resync():
        event = publisher.resync("base_mismatch")
        return jsonify({"sequence": event.sequence, "reason": "base_mismatch"})

    @app.post("/__d4/freeze-today")
    def publish_frozen_today():
        with lock:
            anchor_at = observed_at.replace(hour=11, minute=19, second=50)
            frozen_at = observed_at.replace(hour=11, minute=20, second=0)
            current_at = observed_at.replace(hour=13, minute=30, second=0)
            source = _snapshot("d4-browser-frozen", _feature)
            anchored = tuple(
                replace(
                    item,
                    features=replace(
                        item.features,
                        quote=replace(
                            item.features.quote,
                            source_time=anchor_at,
                            received_time=anchor_at,
                            data_version=f"anchor:{item.features.quote.code}",
                        ),
                    ),
                )
                for item in source.recommendations
            )
            snapshot = replace(
                source,
                trade_date=observed_at.date().isoformat(),
                phase="today_freeze",
                published_at=frozen_at,
                recommendations=anchored,
                frozen=True,
            )
            overlay = LiveOverlay(
                snapshot_id=snapshot.snapshot_id,
                strategy=snapshot.strategy,
                trade_date=snapshot.trade_date,
                version="d4-browser-live-overlay",
                observed_at=current_at,
                quotes={
                    item.features.quote.code: LiveQuote(
                        code=item.features.quote.code,
                        price=13.2,
                        pct_change=5.5,
                        source="tencent",
                        source_time=current_at,
                        received_time=current_at,
                        data_version=f"overlay:{item.features.quote.code}",
                    )
                    for item in snapshot.recommendations
                },
            )
            index.publish(snapshot)
            snapshot_event = publisher.publish(snapshot)
            assert snapshot_event is not None
            index.publish_overlay(overlay)
            overlay_event = publisher.publish_overlay(overlay)
            assert overlay_event is not None
            state["snapshot_id"] = snapshot.snapshot_id
        return jsonify(
            {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_sequence": snapshot_event.sequence,
                "overlay_sequence": overlay_event.sequence,
            }
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5094)
    args = parser.parse_args()
    build_app().run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
