"""Inspect the complete, paired close-return publications from offline diagnostics."""

from importlib.util import find_spec
from pathlib import Path

from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.d25.v1.models import LoadV1ScoringProfile
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import InspectPublishedProfileBundles, ProfilePublicationBusyError
from trader.training.infra.research.close_return.selection import ResearchCloseReturnProfile


class DiagnosticScoringUnavailableError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"diagnostic_scoring_{reason}")


def LoadDiagnosticScoringProfile(profile_id: ScoringProfileId, *, training_root: Path) -> LoadedScoringProfile:
    try:
        if profile_id == "v1":
            return LoadV1ScoringProfile(training_root)
        if profile_id == "v5":
            if find_spec("torch") is None:
                raise DiagnosticScoringUnavailableError("dependency_missing")
            from trader.training.infra.tcn.network import ConfigureTcnRuntime

            ConfigureTcnRuntime(training=False)
        profile = ResearchCloseReturnProfile(profile_id)
        inspected = InspectPublishedProfileBundles(training_root / profile.output_directory, profile)
        return BuildTrainedScoringProfile(profile_id, tuple(item.model for item in inspected))
    except FileNotFoundError as exc:
        raise DiagnosticScoringUnavailableError("missing") from exc
    except ProfilePublicationBusyError as exc:
        raise DiagnosticScoringUnavailableError("publication_busy") from exc
    except ModelBundleIntegrityError as exc:
        raise DiagnosticScoringUnavailableError("integrity_conflict") from exc
    except ModelBundleContractError as exc:
        raise DiagnosticScoringUnavailableError("contract_conflict") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise DiagnosticScoringUnavailableError("corrupt") from exc
