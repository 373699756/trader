from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import time
from typing import Dict, Iterable, List
from zoneinfo import ZoneInfo

from .. import config
from ..normalization import normalize_code
from ..strategies.types import storage_strategy_name
from .cache import DeepSeekCache
from .feature_schema import (
    FEATURE_SCHEMA_VERSION,
    adapt_feature_to_strategy,
    abstain_feature,
    candidate_feature_input,
    prompt_version,
    validate_feature_response,
)
from .research_policy import (
    has_qualitative_evidence,
    neutralize_shared_research_messages,
    qualitative_evidence_hash,
)
from .feature_dependencies import (
    FEATURE_HTTP_CLIENT,
    FEATURE_NEWS_CONTEXT_PROVIDER,
    FEATURE_PAYLOAD_BUILDER,
    deepseek_chat_url,
    feature_runtime_config,
    safe_parse_json,
)


class DeepSeekFeatureAnalysisService:
    """Precompute point-in-time event features outside the recommendation path."""

    @staticmethod
    def _reserve_daily_api_call(store, batch_id, requested_at):
        limit = max(0, int(getattr(config, "DEEPSEEK_DAILY_CALL_LIMIT", 20)))
        if limit == 0:
            return False

        try:
            with store.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                call_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM deepseek_analysis_batches
                    WHERE api_called = 1
                      AND substr(requested_at, 1, 10) = ?
                    """,
                    (requested_at.date().isoformat(),),
                ).fetchone()[0]
                if int(call_count or 0) >= limit:
                    return False
                on_demand_start = str(getattr(config, "DEEPSEEK_ON_DEMAND_START", "14:30"))[:5]
                if requested_at.strftime("%H:%M") < on_demand_start:
                    pre_limit = min(
                        limit,
                        max(0, int(getattr(config, "DEEPSEEK_PRE_1430_CALL_LIMIT", 30))),
                    )
                    pre_count = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM deepseek_analysis_batches
                        WHERE api_called = 1
                          AND substr(requested_at, 1, 10) = ?
                          AND substr(requested_at, 12, 5) < ?
                        """,
                        (requested_at.date().isoformat(), on_demand_start),
                    ).fetchone()[0]
                    if int(pre_count or 0) >= pre_limit:
                        return False
                cursor = conn.execute(
                    """
                    UPDATE deepseek_analysis_batches
                    SET api_called = 1
                    WHERE batch_id = ? AND api_called = 0
                    """,
                    (batch_id,),
                )
                return cursor.rowcount == 1
        except Exception:
            # Fail closed so bookkeeping errors cannot bypass the daily cap.
            return False

    def __init__(self) -> None:
        self.cache = DeepSeekCache()

    def analyze(
        self,
        strategy_name: str,
        candidates: Iterable[Dict[str, object]],
        store,
        *,
        cutoff_at: str,
        snapshot_id: str = "",
        market_filter: str = "all",
        deadline_at: str = "",
        model_tier: str = "flash",
    ) -> Dict[str, object]:
        strategy = storage_strategy_name(strategy_name)
        requested_at = datetime.now()
        rows = [dict(row) for row in candidates or [] if isinstance(row, dict)]
        rows = FEATURE_NEWS_CONTEXT_PROVIDER.attach(rows)
        review_limit = max(1, int(getattr(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 30)))
        pre_1430 = requested_at.strftime("%H:%M") < "14:30"
        call_phase = "pre_1430" if pre_1430 else "post_1430_on_demand"
        phase_limit = int(
            getattr(
                config,
                "DEEPSEEK_PRE_1430_REVIEW_LIMIT" if pre_1430 else "DEEPSEEK_POST_1430_REVIEW_LIMIT",
                12 if pre_1430 else 20,
            )
        )
        review_limit = min(review_limit, max(1, phase_limit))
        rows = rows[:review_limit]
        request = FEATURE_PAYLOAD_BUILDER.feature_request_payload(
            strategy,
            rows,
            market_filter,
            cutoff_at=cutoff_at,
            snapshot_id=snapshot_id,
        )
        feature_candidates = request["candidates"]
        runtime = feature_runtime_config()
        selected_model = str(
            runtime.get("pro_model")
            if model_tier == "pro"
            else getattr(config, "DEEPSEEK_FEATURE_MODEL", runtime.get("model") or "")
        )
        request_hash = hashlib.sha256(
            json.dumps(
                {"request": request, "model": selected_model},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        batch_id = "dsf_{}".format(
            hashlib.sha256(
                "{}|{}|{}|{}".format(strategy, snapshot_id, request_hash, requested_at.isoformat()).encode("utf-8")
            ).hexdigest()[:28]
        )
        batch = {
            "batch_id": batch_id,
            "strategy_name": strategy,
            "snapshot_id": str(snapshot_id or ""),
            "cutoff_at": str(cutoff_at or requested_at.isoformat(timespec="seconds")),
            "prompt_version": prompt_version(strategy),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "model_name": selected_model,
            "model_tier": str(model_tier or "flash"),
            "call_phase": call_phase,
            "review_limit": review_limit,
            "market_filter": str(market_filter or "all"),
            "status": "pending",
            "request_hash": request_hash,
            "candidate_count": len(feature_candidates),
            "requested_at": requested_at.isoformat(timespec="seconds"),
            "created_at": requested_at.isoformat(timespec="seconds"),
        }
        store.save_deepseek_analysis_batch(batch)

        no_evidence = [
            item for item in feature_candidates if not has_qualitative_evidence(item.get("evidence"))
        ]
        reviewable = [
            item for item in feature_candidates if has_qualitative_evidence(item.get("evidence"))
        ]
        neutral_rows = [
            {"feature": abstain_feature(item, strategy, "no_qualitative_point_in_time_evidence"), "valid": True}
            for item in no_evidence
        ]
        if not reviewable:
            return self._finish(store, batch, neutral_rows, status="no_evidence")

        cache_keys = {
            normalize_code(item.get("code")): self._feature_cache_key(strategy, item, selected_model)
            for item in reviewable
        }
        feature_cache = self._read_cache()
        cached_features: List[Dict[str, object]] = []
        uncached_candidates: List[Dict[str, object]] = []
        for item in reviewable:
            code = normalize_code(item.get("code"))
            cached = self._cached(cache_keys.get(code, ""), feature_cache)
            cached_feature = next(
                (
                    dict(value)
                    for value in cached or []
                    if isinstance(value, dict)
                    and normalize_code(value.get("code")) == code
                    and str(value.get("schema_version") or "") == FEATURE_SCHEMA_VERSION
                ),
                None,
            )
            if cached_feature is not None:
                cached_feature["evidence_hash"] = str(item.get("evidence_hash") or "")
                cached_feature = adapt_feature_to_strategy(cached_feature, strategy)
            if cached_feature is None:
                uncached_candidates.append(item)
            else:
                cached_features.append(cached_feature)
        base_feature_rows = neutral_rows + [
            {"feature": item, "valid": True}
            for item in cached_features
        ]
        if not uncached_candidates:
            return self._finish(store, batch, base_feature_rows, status="cache_hit")

        deadline = self._deadline(cutoff_at, deadline_at)
        if deadline is not None and datetime.now() >= deadline:
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="deadline_skipped",
                error_type="deadline",
                error_message="DeepSeek feature deadline reached before request",
            )
        if not bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True)) or not runtime.get("enabled") or not runtime.get("api_key"):
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="partial" if cached_features else "disabled",
                error_type="disabled",
                error_message="DeepSeek feature service is disabled or missing API key",
            )

        if deadline is not None and (deadline - datetime.now()).total_seconds() < 1.0:
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="deadline_skipped",
                error_type="deadline",
                error_message="Insufficient time remained for a production-eligible API request",
            )

        reviewable_codes = {normalize_code(item.get("code")) for item in uncached_candidates}
        reviewable_rows = [row for row in rows if normalize_code(row.get("code")) in reviewable_codes]
        if not self._reserve_daily_api_call(store, batch_id, requested_at):
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="daily_call_limit",
                error_type="daily_call_limit",
                error_message="DeepSeek daily API call limit reached",
            )
        messages = FEATURE_PAYLOAD_BUILDER.build_feature_messages(
            strategy,
            reviewable_rows,
            market_filter,
            cutoff_at=cutoff_at,
            snapshot_id=snapshot_id,
        )
        messages = neutralize_shared_research_messages(messages)
        timeout_seconds = max(1.0, float(runtime.get("timeout_seconds") or 12.0))
        if deadline is not None:
            timeout_seconds = min(timeout_seconds, max(0.5, (deadline - datetime.now()).total_seconds()))
        started = time.monotonic()
        result = FEATURE_HTTP_CLIENT.post_json(
            deepseek_chat_url(str(runtime.get("base_url") or "")),
            headers={
                "Authorization": "Bearer {}".format(runtime.get("api_key")),
                "Content-Type": "application/json",
            },
            payload={
                "model": selected_model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": max(
                    900,
                    int(runtime.get("max_tokens") or 800),
                    500 + len(uncached_candidates) * 230,
                ),
                "response_format": {"type": "json_object"},
            },
            timeout=timeout_seconds,
            retry_count=0,
            retry_base_delay=0.0,
            parse_content=safe_parse_json,
        )
        batch["latency_ms"] = int((time.monotonic() - started) * 1000)
        usage = result.usage or {}
        for key in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens"):
            batch[key] = int(usage.get(key) or 0)
        if result.parsed is None:
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="partial" if cached_features else "error",
                error_type="timeout" if result.timed_out else "api_error",
                error_message=str(result.error or "DeepSeek response unavailable"),
            )

        valid, errors = validate_feature_response(
            result.parsed,
            strategy_name=strategy,
            candidates=uncached_candidates,
        )
        returned_codes = {normalize_code(item.get("code")) for item in valid}
        missing = [
            abstain_feature(item, strategy, "model_result_missing_or_invalid")
            for item in uncached_candidates
            if normalize_code(item.get("code")) not in returned_codes
        ]
        feature_rows = base_feature_rows + [{"feature": item, "valid": True} for item in valid + missing]
        batch["response_hash"] = hashlib.sha256(
            json.dumps(result.raw or result.parsed, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        self._write_cache(
            {
                cache_keys.get(normalize_code(item.get("code")), ""): [item]
                for item in valid
            },
            feature_cache,
        )
        completed_after_deadline = deadline is not None and datetime.now() >= deadline
        return self._finish(
            store,
            batch,
            feature_rows,
            status="late_shadow" if completed_after_deadline else "partial" if errors or missing else "ok",
            rejected_count=len(errors),
            error_type="validation" if errors else "",
            error_message=json.dumps(errors[:10], ensure_ascii=False) if errors else "",
        )

    @staticmethod
    def _deadline(cutoff_at: str, deadline_at: str):
        value = str(deadline_at or getattr(config, "DEEPSEEK_PRECOMPUTE_DEADLINE", "14:48") or "")
        if not value:
            return None
        if "T" in value or " " in value:
            try:
                result = datetime.fromisoformat(value.replace(" ", "T", 1))
                if result.tzinfo is not None:
                    result = result.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                return result
            except Exception:
                return None
        day = str(cutoff_at or datetime.now().date().isoformat())[:10]
        try:
            return datetime.fromisoformat("{}T{}:00".format(day, value[:5]))
        except Exception:
            return None

    @staticmethod
    def _feature_cache_key(strategy: str, candidate: Dict[str, object], model_name: str) -> str:
        payload = {
            "code": normalize_code(candidate.get("code")),
            "qualitative_evidence_hash": qualitative_evidence_hash(candidate.get("evidence")),
            "verified_risk_flags": candidate.get("verified_risk_flags") or [],
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "shared_prompt_version": "shared_research_v2",
            "model": str(model_name or ""),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _read_cache(self) -> Dict[str, object]:
        if not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return {}
        path = str(getattr(config, "DEEPSEEK_FEATURE_CACHE_PATH", ".runtime/deepseek_feature_cache.json") or "")
        return self.cache.read(path)

    def _cached(self, request_hash: str, cache: Dict[str, object] = None):
        if not request_hash or not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return None
        entry = (cache if isinstance(cache, dict) else self._read_cache()).get(request_hash)
        if not self.cache.entry_valid(
            entry,
            int(getattr(config, "DEEPSEEK_FEATURE_CACHE_TTL_SECONDS", 21600)),
            schema_version=1,
        ):
            return None
        return entry.get("features") if isinstance(entry.get("features"), list) else None

    def _write_cache(
        self,
        entries: Dict[str, List[Dict[str, object]]],
        cache: Dict[str, object] = None,
    ) -> None:
        entries = {key: value for key, value in (entries or {}).items() if key}
        if not entries or not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return
        path = str(getattr(config, "DEEPSEEK_FEATURE_CACHE_PATH", ".runtime/deepseek_feature_cache.json") or "")
        cache = dict(cache) if isinstance(cache, dict) else self.cache.read(path)
        cached_at = time.time()
        for request_hash, features in entries.items():
            cache[request_hash] = {
                "schema": 1,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "cached_at": cached_at,
                "features": features,
            }
        max_entries = max(10, int(getattr(config, "DEEPSEEK_FEATURE_CACHE_MAX_ENTRIES", 2000)))
        if len(cache) > max_entries:
            ordered = sorted(cache.items(), key=lambda item: float((item[1] or {}).get("cached_at") or 0), reverse=True)
            cache = dict(ordered[:max_entries])
        self.cache.merge(path, {request_hash: cache[request_hash] for request_hash in entries})

    @staticmethod
    def _finish(
        store,
        batch: Dict[str, object],
        rows: List[Dict[str, object]],
        *,
        status: str,
        rejected_count: int = 0,
        error_type: str = "",
        error_message: str = "",
    ) -> Dict[str, object]:
        completed = datetime.now()
        result_batch = dict(batch)
        result_batch.update(
            {
                "status": status,
                "completed_at": completed.isoformat(timespec="seconds"),
                "expires_at": (completed + timedelta(hours=6)).isoformat(timespec="seconds"),
                "valid_count": sum(1 for row in rows if row.get("valid")),
                "abstain_count": sum(1 for row in rows if (row.get("feature") or {}).get("abstain")),
                "rejected_count": int(rejected_count),
                "error_type": str(error_type or ""),
                "error_message": str(error_message or "")[:1000],
            }
        )
        if rows:
            for row in rows:
                row.setdefault("completed_at", result_batch["completed_at"])
                row.setdefault("expires_at", result_batch["expires_at"])
            store.save_deepseek_candidate_features(result_batch, rows)
        store.save_deepseek_analysis_batch(result_batch)
        return {
            "batch_id": result_batch["batch_id"],
            "strategy": result_batch["strategy_name"],
            "status": status,
            "candidate_count": result_batch["candidate_count"],
            "valid_count": result_batch["valid_count"],
            "abstain_count": result_batch["abstain_count"],
            "rejected_count": result_batch["rejected_count"],
            "completed_at": result_batch["completed_at"],
            "error": result_batch["error_message"],
        }
