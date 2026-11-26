"""Compare repeated startup reads of fixed external V2/V3 D25 publications."""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from scripts.core.paths import PROJECT_ROOT
from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash, FileSha256
from trader.training.infra.d25 import model_document
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.history.profile_inspection import StartupProfileInspections
from trader.training.infra.model_bundles.profile_publication import InspectPublishedProfileBundles


def CompareStartupInspections(root: Path, rounds: int) -> dict[str, object]:
    if root.resolve().is_relative_to(PROJECT_ROOT) or not 1 <= rounds <= 12:
        raise ValueError("expected an external sealed model copy and 1..12 rounds")
    profiles = (StandardD25Profile("v2"), StandardD25Profile("v3")) * 4
    rows = []
    expected = None
    files = tuple(root / lane / "d25" / name for lane in ("v2", "v3") for name in ("model.json", "metadata.json"))
    before = tuple(FileSha256(file) for file in files)
    for ordinal in range(rounds):
        for candidate in ((False, True) if ordinal % 2 == 0 else (True, False)):
            cache = StartupProfileInspections()
            calls = size = 0
            identities: list[tuple[object, ...]] = []

            def read(path: Path, *, maximum_bytes: int) -> bytes:
                nonlocal calls, size
                result = ReadRegularFile(path, maximum_bytes=maximum_bytes)
                calls += 1
                size += len(result)
                return result

            with patch.object(model_document, "ReadRegularFile", read):
                start, cpu = time.perf_counter(), time.process_time()
                for profile in profiles:
                    directory = root / profile.output_directory
                    heads = (cache.inspect(directory, profile) if candidate
                             else InspectPublishedProfileBundles(directory, profile))
                    identities.extend((
                        str(head.model_path.relative_to(root)), head.strategy.value, head.training_input_hash,
                        head.source_identity_hash, head.label_cutoff, head.training_contract_hash, head.model_hash,
                        head.report_hash, head.training_input_document_hash, head.metadata_hash,
                        head.training_run_hash, head.price_snapshot_hash,
                    ) for head in heads)
                elapsed, used = time.perf_counter() - start, time.process_time() - cpu
            digest = ContentHash(identities)
            if expected is not None and digest != expected:
                raise ValueError("startup_inspection_observations_differ")
            expected = digest
            rows.append({"candidate": candidate, "round": ordinal + 1, "wall_seconds": elapsed,
                         "cpu_seconds": used, "regular_file_reads": calls, "regular_file_bytes": size})
    if before != tuple(FileSha256(file) for file in files):
        raise ValueError("startup_inspection_input_changed")
    return {"input_sha256": before, "receipt_sha256": expected, "runs": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    options = parser.parse_args()
    print(json.dumps(CompareStartupInspections(options.models, options.rounds)))
