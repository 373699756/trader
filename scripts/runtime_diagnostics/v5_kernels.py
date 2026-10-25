"""Bounded synthetic float64 comparison against a frozen Conv1d reference."""

from __future__ import annotations

import copy
import statistics
import sys
import time
from typing import cast

import torch
from torch.nn import functional

from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork


class ReferenceTcnNetwork(TcnNetwork):
    """Original Conv1d execution, only for diagnostics and parity tests."""

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        auxiliary = values[:, : self.auxiliary_width]
        hidden = values[:, self.auxiliary_width :].reshape(-1, 60, 6).transpose(1, 2)
        for block in self.blocks:
            before = hidden
            hidden = block.dropout(functional.relu(block.first(functional.pad(hidden, (block.padding, 0)))))
            hidden = block.dropout(functional.relu(block.second(functional.pad(hidden, (block.padding, 0)))))
            hidden = functional.relu(hidden + block.shortcut(before))
        return cast(torch.Tensor, self.output(torch.cat((hidden[:, :, -1], auxiliary), dim=1)).flatten())


def _step(network: TcnNetwork, optimizer: torch.optim.Optimizer, values: torch.Tensor, labels: torch.Tensor) -> None:
    optimizer.zero_grad(set_to_none=True)
    torch.square(network(values) - labels).mean().backward()
    optimizer.step()


def _profile(network: TcnNetwork, values: torch.Tensor, labels: torch.Tensor) -> list[dict[str, object]]:
    optimizer = torch.optim.AdamW(network.parameters(), lr=0.001, weight_decay=0.0001)
    with torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU]) as observed:
        _step(network, optimizer, values, labels)
    events = sorted(observed.key_averages(), key=lambda event: event.self_cpu_time_total, reverse=True)
    return [
        {"operator": event.key, "self_cpu_ms": round(event.self_cpu_time_total / 1000, 3), "calls": event.count}
        for event in events[:10]
    ]


def _peak_rss_mib() -> float | None:
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024**2 if sys.platform == "darwin" else 1024)


def ProbeV5Kernels() -> dict[str, object]:
    """Measure paired whole-network updates without loading any history or model files."""
    ConfigureTcnRuntime(training=True)
    torch.manual_seed(0)
    values = torch.randn(256, 366, dtype=torch.float64)
    labels = torch.randn(256, dtype=torch.float64)
    candidate = TcnNetwork(6, 0.1)
    reference = ReferenceTcnNetwork(6, 0.1)
    reference.load_state_dict(candidate.state_dict())
    left = copy.deepcopy(reference)
    right = copy.deepcopy(candidate)
    left_optimizer = torch.optim.AdamW(left.parameters(), lr=0.001, weight_decay=0.0001)
    right_optimizer = torch.optim.AdamW(right.parameters(), lr=0.001, weight_decay=0.0001)
    for step in range(3):
        torch.manual_seed(step)
        _step(left, left_optimizer, values, labels)
        torch.manual_seed(step)
        _step(right, right_optimizer, values, labels)
    weight_error = max(
        float((a - b).abs().max().detach()) for a, b in zip(left.parameters(), right.parameters(), strict=True)
    )
    for a, b in zip(left.parameters(), right.parameters(), strict=True):
        torch.testing.assert_close(a, b, rtol=1e-9, atol=1e-11)
    left.eval()
    right.eval()
    with torch.no_grad():
        left_prediction, right_prediction = left(values), right(values)
        torch.testing.assert_close(left_prediction, right_prediction, rtol=1e-9, atol=1e-11)
        prediction_error = float((left_prediction - right_prediction).abs().max())
    profiles = {
        "reference": _profile(copy.deepcopy(reference), values, labels),
        "candidate": _profile(copy.deepcopy(candidate), values, labels),
    }
    comparisons = []
    speedups = []
    for threads in (1, 2):
        torch.set_num_threads(threads)
        times: dict[str, list[float]] = {"reference": [], "candidate": []}
        for round_number in range(5):
            order = ("reference", "candidate") if round_number % 2 == 0 else ("candidate", "reference")
            for name in order:
                model = copy.deepcopy(reference if name == "reference" else candidate)
                optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.0001)
                torch.manual_seed(100)
                _step(model, optimizer, values, labels)
                started = time.perf_counter()
                for _ in range(3):
                    _step(model, optimizer, values, labels)
                times[name].append((time.perf_counter() - started) / 3)
        speedup = statistics.median(a / b for a, b in zip(times["reference"], times["candidate"], strict=True))
        speedups.append(speedup)
        comparisons.append(
            {
                "threads": threads,
                "reference_seconds_per_batch": times["reference"],
                "candidate_seconds_per_batch": times["candidate"],
                "median_paired_speedup": speedup,
            }
        )
    return {
        "schema_version": "v5-kernel-diagnostic",
        "status": "passed" if all(speedup >= 1 for speedup in speedups) else "degraded",
        "summary": {
            "torch_version": torch.__version__,
            "dtype": "float64",
            "batch_size": 256,
            "sequence_steps": 60,
            "auxiliary_features": 6,
            "measured_rounds": 5,
            "numerical_parity_passed": True,
            "performance_gate": "median_paired_speedup_at_least_one_for_each_thread_count",
            "batches_per_round": 3,
            "maximum_parameter_error_after_three_updates": weight_error,
            "maximum_prediction_error_after_three_updates": prediction_error,
            "operators_two_threads": profiles,
            "comparisons": comparisons,
            "peak_rss_mib": _peak_rss_mib(),
            "history_reads": 0,
            "production_authority": False,
        },
    }


__all__ = ["ProbeV5Kernels", "ReferenceTcnNetwork"]
