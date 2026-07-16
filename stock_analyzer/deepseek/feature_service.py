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
from .budget import candidate_budget_priority, phase_at, reserve_api_call
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
        reservation = reserve_api_call(store, batch_id, "shared_preheat", requested_at)
        return reservation.allowed

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
        emergency: bool = False,
    ) -> Dict[str, object]:
        strategy = storage_strategy_name(strategy_name)
        requested_at = datetime.now()
        rows = [dict(row) for row in candidates or [] if isinstance(row, dict)]
        rows = FEATURE_NEWS_CONTEXT_PROVIDER.attach(rows)
        review_limit = max(1, int(getattr(config, "DEEPSEEK_FEATURE_REVIEW_LIMIT", 30)))
        call_phase = phase_at(requested_at, emergency=emergency)
        phase_limit = (
            int(getattr(config, "DEEPSEEK_POST_1430_REVIEW_LIMIT", 38))
            if call_phase == "final_supplement"
            else int(getattr(config, "DEEPSEEK_PRE_1430_REVIEW_LIMIT", 30))
        )
        review_limit = min(review_limit, max(1, phase_limit))
        indexed_rows = list(enumerate(rows))
        indexed_rows.sort(
            key=lambda item: candidate_budget_priority(item[1], item[0]),
            reverse=True,
        )
        rows = [row for _, row in indexed_rows[:review_limit]]
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
        request_hash = self._request_hash(request, selected_model)
        batch_prefix = "dsf_{}".format(
            hashlib.sha256(
                "{}|{}|{}|{}".format(strategy, snapshot_id, request_hash, requested_at.isoformat()).encode("utf-8")
            ).hexdigest()[:24]
        )
        batch = {
            "batch_id": "{}_summary".format(batch_prefix),
            "strategy_name": strategy,
            "snapshot_id": str(snapshot_id or ""),
            "cutoff_at": str(cutoff_at or requested_at.isoformat(timespec="seconds")),
            "prompt_version": prompt_version(strategy),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "model_name": selected_model,
            "model_tier": str(model_tier or "flash"),
            "call_phase": call_phase,
            "budget_bucket": "",
            "review_limit": review_limit,
            "market_filter": str(market_filter or "all"),
            "status": "pending",
            "request_hash": request_hash,
            "candidate_count": len(feature_candidates),
            "requested_at": requested_at.isoformat(timespec="seconds"),
            "created_at": requested_at.isoformat(timespec="seconds"),
        }

        no_evidence = [item for item in feature_candidates if not has_qualitative_evidence(item.get("evidence"))]
        reviewable = [item for item in feature_candidates if has_qualitative_evidence(item.get("evidence"))]
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
            cached = self._cached(cache_keys.get(code, ""), feature_cache, candidate=item)
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
        base_feature_rows = neutral_rows + [{"feature": item, "valid": True} for item in cached_features]
        if not uncached_candidates:
            return self._finish(store, batch, base_feature_rows, status="cache_hit")

        # Emergency reviews use their own all-day reserve and are not subject to
        # the normal 14:48 production precompute deadline.
        deadline = None if emergency else self._deadline(cutoff_at, deadline_at)
        if deadline is not None and datetime.now() >= deadline:
            return self._finish(
                store,
                batch,
                base_feature_rows,
                status="deadline_skipped",
                error_type="deadline",
                error_message="DeepSeek feature deadline reached before request",
            )
        if (
            not bool(getattr(config, "ENABLE_DEEPSEEK_FEATURES", True))
            or not runtime.get("enabled")
            or not runtime.get("api_key")
        ):
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

        batch_size = max(1, min(8, int(getattr(config, "DEEPSEEK_FEATURE_API_BATCH_SIZE", 8))))
        rows_by_code = {normalize_code(row.get("code")): row for row in rows if normalize_code(row.get("code"))}
        feature_rows = list(base_feature_rows)
        batch_ids: List[str] = []
        api_call_count = 0
        rejected_count = 0
        response_hashes: List[str] = []
        validation_errors: List[Dict[str, str]] = []
        had_partial_response = False
        terminal_status = ""
        terminal_error_type = ""
        terminal_error_message = ""
        on_time_reviewed = len(cached_features)
        token_keys = ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens")
        token_totals = {key: 0 for key in token_keys}
        latency_total = 0

        if base_feature_rows:
            base_batch = dict(batch)
            base_batch.update(
                batch_id="{}_base".format(batch_prefix),
                request_hash=hashlib.sha256("{}|base".format(request_hash).encode("utf-8")).hexdigest(),
                candidate_count=len(base_feature_rows),
            )
            base_result = self._finish(
                store,
                base_batch,
                base_feature_rows,
                status="cache_hit" if cached_features else "no_evidence",
            )
            batch_ids.append(str(base_result["batch_id"]))

        for batch_index, start in enumerate(range(0, len(uncached_candidates), batch_size), start=1):
            candidate_batch = uncached_candidates[start : start + batch_size]
            now = datetime.now()
            if deadline is not None and (deadline - now).total_seconds() < 1.0:
                terminal_status = "deadline_skipped"
                terminal_error_type = "deadline"
                terminal_error_message = "Insufficient time remained for another production-eligible API request"
                break

            candidate_codes = [normalize_code(item.get("code")) for item in candidate_batch]
            request_rows = [rows_by_code[code] for code in candidate_codes if code in rows_by_code]
            child_request = FEATURE_PAYLOAD_BUILDER.feature_request_payload(
                strategy,
                request_rows,
                market_filter,
                cutoff_at=cutoff_at,
                snapshot_id=snapshot_id,
            )
            child_batch = dict(batch)
            child_batch.update(
                batch_id="{}_call_{:03d}".format(batch_prefix, batch_index),
                request_hash=self._request_hash(child_request, selected_model),
                candidate_count=len(candidate_batch),
                status="pending",
            )
            store.save_deepseek_analysis_batch(child_batch)
            batch_ids.append(str(child_batch["batch_id"]))

            reservation_at = datetime.now()
            reservation = reserve_api_call(
                store,
                str(child_batch["batch_id"]),
                strategy,
                reservation_at,
                emergency=emergency,
            )
            child_batch["call_phase"] = reservation.phase
            child_batch["budget_bucket"] = reservation.budget_bucket
            if not reservation.allowed:
                self._finish(
                    store,
                    child_batch,
                    [],
                    status=reservation.status,
                    error_type=reservation.status,
                    error_message=reservation.reason or "DeepSeek API budget unavailable",
                )
                terminal_status = reservation.status
                terminal_error_type = reservation.status
                terminal_error_message = reservation.reason or "DeepSeek API budget unavailable"
                break

            messages = FEATURE_PAYLOAD_BUILDER.build_feature_messages(
                strategy,
                request_rows,
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
                        500 + len(candidate_batch) * 230,
                    ),
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout_seconds,
                retry_count=0,
                retry_base_delay=0.0,
                parse_content=safe_parse_json,
            )
            api_call_count += 1
            child_batch["latency_ms"] = int((time.monotonic() - started) * 1000)
            latency_total += int(child_batch["latency_ms"])
            usage = result.usage or {}
            for key in token_keys:
                child_batch[key] = int(usage.get(key) or 0)
                token_totals[key] += int(child_batch[key])
            if result.parsed is None:
                error_type = "timeout" if result.timed_out else "api_error"
                error_message = str(result.error or "DeepSeek response unavailable")
                self._finish(
                    store,
                    child_batch,
                    [],
                    status="error",
                    error_type=error_type,
                    error_message=error_message,
                )
                terminal_status = "error"
                terminal_error_type = error_type
                terminal_error_message = error_message
                break

            valid, errors = validate_feature_response(
                result.parsed,
                strategy_name=strategy,
                candidates=candidate_batch,
            )
            returned_codes = {normalize_code(item.get("code")) for item in valid}
            missing = [
                abstain_feature(item, strategy, "model_result_missing_or_invalid")
                for item in candidate_batch
                if normalize_code(item.get("code")) not in returned_codes
            ]
            child_feature_rows = [{"feature": item, "valid": True} for item in valid + missing]
            child_batch["response_hash"] = hashlib.sha256(
                json.dumps(result.raw or result.parsed, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            response_hashes.append(str(child_batch["response_hash"]))
            self._write_cache(
                {cache_keys.get(normalize_code(item.get("code")), ""): [item] for item in valid},
                feature_cache,
                contexts={cache_keys.get(normalize_code(item.get("code")), ""): item for item in candidate_batch},
            )
            completed_after_deadline = deadline is not None and datetime.now() >= deadline
            child_status = "late_shadow" if completed_after_deadline else "partial" if errors or missing else "ok"
            self._finish(
                store,
                child_batch,
                child_feature_rows,
                status=child_status,
                rejected_count=len(errors),
                error_type="validation" if errors else "",
                error_message=json.dumps(errors[:10], ensure_ascii=False) if errors else "",
            )
            feature_rows.extend(child_feature_rows)
            rejected_count += len(errors)
            validation_errors.extend(errors)
            if not completed_after_deadline:
                on_time_reviewed += len(child_feature_rows)
            had_partial_response = had_partial_response or bool(errors or missing)
            if completed_after_deadline:
                terminal_status = "late_shadow"
                terminal_error_type = "deadline"
                terminal_error_message = "DeepSeek response completed after the production deadline"
                break

        if terminal_status:
            if terminal_status == "late_shadow":
                summary_status = "partial" if on_time_reviewed else "late_shadow"
            else:
                summary_status = "partial" if on_time_reviewed else terminal_status
            summary_error_type = terminal_error_type
            summary_error_message = terminal_error_message
        elif had_partial_response:
            summary_status = "partial"
            summary_error_type = "validation" if validation_errors else ""
            summary_error_message = (
                json.dumps(validation_errors[:10], ensure_ascii=False)
                if validation_errors
                else "model_result_missing_or_invalid"
            )
        else:
            summary_status = "ok"
            summary_error_type = ""
            summary_error_message = ""

        batch.update(token_totals)
        batch["latency_ms"] = latency_total
        if response_hashes:
            batch["response_hash"] = hashlib.sha256("|".join(response_hashes).encode("utf-8")).hexdigest()
        summary = self._finish(
            store,
            batch,
            [],
            status=summary_status,
            rejected_count=rejected_count,
            error_type=summary_error_type,
            error_message=summary_error_message,
            valid_count=sum(1 for row in feature_rows if row.get("valid")),
            abstain_count=sum(1 for row in feature_rows if (row.get("feature") or {}).get("abstain")),
        )
        batch_ids.append(str(summary["batch_id"]))
        summary["batch_ids"] = batch_ids
        summary["api_call_count"] = api_call_count
        return summary

    @staticmethod
    def _request_hash(request: Dict[str, object], model_name: str) -> str:
        return hashlib.sha256(
            json.dumps(
                {"request": request, "model": str(model_name or "")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

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
            "evidence_hash": str(
                candidate.get("evidence_hash") or qualitative_evidence_hash(candidate.get("evidence"))
            ),
            "verified_risk_flags": candidate.get("verified_risk_flags") or [],
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "prompt_version": prompt_version(strategy),
            "model": str(model_name or ""),
            "research_input_version": str(candidate.get("research_input_version") or ""),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _read_cache(self) -> Dict[str, object]:
        if not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return {}
        path = str(getattr(config, "DEEPSEEK_FEATURE_CACHE_PATH", ".runtime/deepseek_feature_cache.json") or "")
        return self.cache.read(path)

    def _cached(
        self,
        request_hash: str,
        cache: Dict[str, object] = None,
        *,
        candidate: Dict[str, object] = None,
    ):
        if not request_hash or not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return None
        entry = (cache if isinstance(cache, dict) else self._read_cache()).get(request_hash)
        if not self.cache.entry_valid(
            entry,
            int(getattr(config, "DEEPSEEK_FEATURE_CACHE_TTL_SECONDS", 600)),
            schema_version=1,
        ):
            return None
        if isinstance(candidate, dict):
            cached_score = float(entry.get("local_score") or 0.0)
            current_score = float(candidate.get("local_score") or 0.0)
            score_delta = abs(current_score - cached_score)
            if score_delta > 8.0:
                return None
            market_changed = str(entry.get("market_state_hash") or "") != str(candidate.get("market_state_hash") or "")
            if market_changed and score_delta >= 5.0:
                return None
        return entry.get("features") if isinstance(entry.get("features"), list) else None

    def _write_cache(
        self,
        entries: Dict[str, List[Dict[str, object]]],
        cache: Dict[str, object] = None,
        *,
        contexts: Dict[str, Dict[str, object]] = None,
    ) -> None:
        entries = {key: value for key, value in (entries or {}).items() if key}
        if not entries or not bool(getattr(config, "DEEPSEEK_FEATURE_CACHE_ENABLED", True)):
            return
        path = str(getattr(config, "DEEPSEEK_FEATURE_CACHE_PATH", ".runtime/deepseek_feature_cache.json") or "")
        cached_at = time.time()
        contexts = contexts if isinstance(contexts, dict) else {}
        updates = {}
        for request_hash, features in entries.items():
            context = contexts.get(request_hash) if isinstance(contexts.get(request_hash), dict) else {}
            updates[request_hash] = {
                "schema": 1,
                "date": datetime.now().strftime("%Y-%m-%d"),
                "cached_at": cached_at,
                "features": features,
                "local_score": float(context.get("local_score") or 0.0),
                "market_state_hash": str(context.get("market_state_hash") or ""),
            }
        max_entries = max(10, int(getattr(config, "DEEPSEEK_FEATURE_CACHE_MAX_ENTRIES", 2000)))
        self.cache.merge(path, updates, max_entries=max_entries)

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
        valid_count: int | None = None,
        abstain_count: int | None = None,
    ) -> Dict[str, object]:
        completed = datetime.now()
        result_batch = dict(batch)
        result_batch.update(
            {
                "status": status,
                "completed_at": completed.isoformat(timespec="seconds"),
                "expires_at": (completed + timedelta(hours=6)).isoformat(timespec="seconds"),
                "valid_count": (
                    int(valid_count) if valid_count is not None else sum(1 for row in rows if row.get("valid"))
                ),
                "abstain_count": (
                    int(abstain_count)
                    if abstain_count is not None
                    else sum(1 for row in rows if (row.get("feature") or {}).get("abstain"))
                ),
                "rejected_count": int(rejected_count),
                "error_type": str(error_type or ""),
                "error_message": str(error_message or "")[:1000],
            }
        )
        store.save_deepseek_analysis_batch(result_batch)
        if rows:
            for row in rows:
                row.setdefault("completed_at", result_batch["completed_at"])
                row.setdefault("expires_at", result_batch["expires_at"])
            store.save_deepseek_candidate_features(result_batch, rows)
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
