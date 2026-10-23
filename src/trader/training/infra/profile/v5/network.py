"""Optional CPU tensor adapter; imported only for explicitly selected V5 work."""

from __future__ import annotations

from typing import cast

import numpy as np
import torch
from torch import nn
from torch.nn import functional as functional

from trader.training.infra.profile.v5.tensors import TcnTensor


class _ResidualBlock(nn.Module):
    def __init__(self, inputs: int, dilation: int, dropout: float) -> None:
        super().__init__()
        self.first = nn.Conv1d(inputs, 16, 3, dilation=dilation)
        self.second = nn.Conv1d(16, 16, 3, dilation=dilation)
        self.shortcut = nn.Conv1d(inputs, 16, 1) if inputs != 16 else nn.Identity()
        self.dropout = nn.Dropout(dropout)
        self.padding = 2 * dilation

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        hidden = self.dropout(functional.relu(self.first(functional.pad(values, (self.padding, 0)))))
        hidden = self.dropout(functional.relu(self.second(functional.pad(hidden, (self.padding, 0)))))
        return functional.relu(hidden + self.shortcut(values))


class TcnNetwork(nn.Module):
    def __init__(self, auxiliary_width: int, dropout: float) -> None:
        super().__init__()
        self.blocks = nn.ModuleList(_ResidualBlock(6 if i == 0 else 16, 2**i, dropout) for i in range(4))
        self.output = nn.Linear(16 + auxiliary_width, 1)
        self.auxiliary_width = auxiliary_width
        self.double()

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
            {t.name: torch.tensor(t.values, dtype=torch.float64).reshape(t.shape) for t in tensors}, strict=True
        )

    def predict(self, features: np.ndarray) -> np.ndarray:
        self.eval()
        with torch.inference_mode():
            predicted = cast(torch.Tensor, self(torch.from_numpy(np.ascontiguousarray(features))))
            return np.asarray(predicted.numpy(), dtype=np.float64)


def ConfigureTcnRuntime(*, training: bool) -> None:
    """The composition entrypoint owns process-wide compute settings."""
    torch.set_num_threads(2 if training else 1)
    torch.use_deterministic_algorithms(True)


__all__ = ["TcnNetwork", "ConfigureTcnRuntime"]
