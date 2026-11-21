"""D25 V3 Ridge regression kernel for independent utility estimation."""

import numpy as np


def FitD25UtilityRidge(features: np.ndarray, labels: np.ndarray, penalty_value: float) -> np.ndarray:
    width = features.shape[1] + 1
    penalty = np.eye(width, dtype=np.float64) * penalty_value
    penalty[0, 0] = 0.0
    gram = np.empty((width, width), dtype=np.float64)
    sums = features.sum(axis=0)
    gram[0, 0] = len(features)
    gram[0, 1:] = sums
    gram[1:, 0] = sums
    gram[1:, 1:] = features.T @ features
    target = np.empty(width, dtype=np.float64)
    target[0] = labels.sum()
    target[1:] = features.T @ labels
    return np.linalg.solve(gram + penalty, target)
