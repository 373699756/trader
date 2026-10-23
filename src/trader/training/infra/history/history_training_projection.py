"""Whitelisted maintenance output shared by CLI and recommendation observability adapters."""

from trader.training.domain.model_publication import HeadTrainingStatus, ModelPublicationIdentity


def ProjectHeadTrainingStatus(status: HeadTrainingStatus) -> dict[str, object]:
    due = status.due
    return {
        "profile_id": status.profile_id,
        "strategy": status.strategy.value,
        "owned": status.owned,
        "loaded": _identity(status.loaded),
        "disk": _identity(status.disk),
        "restart_required": status.restart_required,
        "training_due": due.training_due if due else None,
        "training_due_reason": due.reason if due else "unavailable",
        "current_label_cutoff": due.current_label_cutoff.isoformat() if due and due.current_label_cutoff else None,
        "matured_label_days_since_training": due.matured_label_days_since_training if due else None,
        "failure": status.failure,
    }


def _identity(value: ModelPublicationIdentity | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "model_hash": value.model_hash,
        "publication_hash": value.metadata_hash,
        "label_cutoff": value.label_cutoff.isoformat(),
        "training_history_hash": value.training_history_hash,
        "training_run_hash": value.training_run_hash,
    }


__all__ = ["ProjectHeadTrainingStatus"]
