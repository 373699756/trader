"""Optional CPU tensor adapter; imported only for explicitly selected V5 work."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import numpy as np
import torch
from torch import nn
from torch.nn import functional as functional

from trader.training.infra.profile.v5.settings import TcnArchitecture
from trader.training.infra.profile.v5.tensors import TcnTensor


class _ResidualBlock(nn.Module):
    def __init__(self, inputs: int, channels: int, dilation: int, dropout: float) -> None:
        super().__init__()
        self.first = nn.Conv1d(inputs, channels, 3, dilation=dilation)
        self.second = nn.Conv1d(channels, channels, 3, dilation=dilation)
        self.shortcut = nn.Conv1d(inputs, channels, 1) if inputs != channels else nn.Identity()
        self.dropout = nn.Dropout(dropout)
        self.padding = 2 * dilation

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        hidden = self.dropout(functional.relu(self.first(functional.pad(values, (self.padding, 0)))))
        hidden = self.dropout(functional.relu(self.second(functional.pad(hidden, (self.padding, 0)))))
        return functional.relu(hidden + self.shortcut(values))


class TcnNetwork(nn.Module):
    def __init__(self, auxiliary_width: int, dropout: float, architecture: TcnArchitecture) -> None:
        super().__init__()
        channels = architecture.hidden_channels
        self.blocks = nn.ModuleList(
            _ResidualBlock(6 if i == 0 else channels, channels, 2**i, dropout) for i in range(4)
        )
        self.output = nn.Linear(channels + auxiliary_width, 1)
        self.auxiliary_width = auxiliary_width
        self.architecture = architecture
        self.compute_dtype = torch.float32 if architecture.compute_dtype == "float32" else torch.float64
        self.to(dtype=self.compute_dtype)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        auxiliary = values[:, : self.auxiliary_width]
        hidden = values[:, self.auxiliary_width :].reshape(-1, 60, 6).transpose(1, 2)
        for block in self.blocks:
            hidden = block(hidden)
        return cast(torch.Tensor, self.output(torch.cat((hidden[:, :, -1], auxiliary), dim=1)).flatten())

    def tensors(self) -> tuple[TcnTensor, ...]:
        return tuple(
            TcnTensor(name, tuple(t.shape), tuple(t.detach().cpu().flatten().tolist()))
            for name, t in sorted(self.state_dict().items())
        )

    def load_tensors(self, tensors: tuple[TcnTensor, ...]) -> None:
        self.load_state_dict(
            {t.name: torch.tensor(t.values, dtype=self.compute_dtype).reshape(t.shape) for t in tensors}, strict=True
        )

    def predict(self, features: np.ndarray) -> np.ndarray:
        self.eval()
        with torch.inference_mode():
            predicted = cast(
                torch.Tensor, self(torch.from_numpy(np.ascontiguousarray(features)).to(self.compute_dtype))
            )
            return np.asarray(predicted.numpy(), dtype=np.float64)


@contextmanager
def PreserveTcnRuntime() -> Iterator[None]:
    """Restore native policy only after the owning training job has joined its workers."""
    threads = torch.get_num_threads()
    deterministic = torch.are_deterministic_algorithms_enabled()
    warn_only = torch.is_deterministic_algorithms_warn_only_enabled()
    try:
        yield
    finally:
        torch.set_num_threads(threads)
        torch.use_deterministic_algorithms(deterministic, warn_only=warn_only)


def ConfigureTcnRuntime(*, training: bool, compute_threads: int = 2) -> None:
    """Apply the caller's granted thread count in the thread that executes the network."""
    available = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    if training and (type(compute_threads) is not int or not 1 <= compute_threads <= min(4, available)):
        raise ValueError("V5 compute_threads exceeds available CPU capacity")
    torch.set_num_threads(compute_threads if training else 1)
    torch.use_deterministic_algorithms(True)


__all__ = ["TcnNetwork", "ConfigureTcnRuntime", "PreserveTcnRuntime"]
