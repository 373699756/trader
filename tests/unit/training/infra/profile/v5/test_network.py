"""Frozen Conv1d reference for float64 outputs, dropout and gradients."""

from importlib.util import find_spec

import pytest

pytestmark = pytest.mark.skipif(find_spec("torch") is None, reason="V5 requires the optional CPU tensor dependency")


@pytest.mark.parametrize("batch,auxiliary", ((1, 5), (17, 5), (1, 6), (17, 6), (256, 6)))
@pytest.mark.parametrize("training", (False, True), ids=("inference", "dropout"))
def test_float64_network_matches_reference_outputs_input_and_parameter_gradients(batch, auxiliary, training):
    import torch

    from scripts.runtime_diagnostics.v5_kernels import ReferenceTcnNetwork
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    ConfigureTcnRuntime(training=True)
    torch.manual_seed(7)
    candidate = TcnNetwork(auxiliary, 0.1)
    reference = ReferenceTcnNetwork(auxiliary, 0.1)
    reference.load_state_dict(candidate.state_dict())
    candidate.train(training)
    reference.train(training)
    values = torch.randn(batch, auxiliary + 360, dtype=torch.float64, requires_grad=True)
    copied = values.detach().clone().requires_grad_()
    torch.manual_seed(11)
    expected = reference(values)
    expected.square().mean().backward()
    torch.manual_seed(11)
    actual = candidate(copied)
    actual.square().mean().backward()
    torch.testing.assert_close(actual, expected, rtol=1e-10, atol=1e-12)
    torch.testing.assert_close(copied.grad, values.grad, rtol=1e-10, atol=1e-12)
    for (name, parameter), (old_name, old_parameter) in zip(
        candidate.named_parameters(), reference.named_parameters(), strict=True
    ):
        assert name == old_name and parameter.dtype == torch.float64
        torch.testing.assert_close(parameter.grad, old_parameter.grad, rtol=1e-10, atol=1e-12)


@pytest.mark.parametrize("auxiliary", (5, 6), ids=("d25", "tomorrow"))
def test_float64_network_preserves_seeded_adam_updates_and_roundtrip(auxiliary):
    import torch

    from scripts.runtime_diagnostics.v5_kernels import ReferenceTcnNetwork
    from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork

    ConfigureTcnRuntime(training=True)
    torch.manual_seed(7)
    candidate = TcnNetwork(auxiliary, 0.1)
    reference = ReferenceTcnNetwork(auxiliary, 0.1)
    reference.load_state_dict(candidate.state_dict())
    left = torch.optim.AdamW(reference.parameters(), lr=0.001, weight_decay=0.0001)
    right = torch.optim.AdamW(candidate.parameters(), lr=0.001, weight_decay=0.0001)
    values = torch.randn(17, auxiliary + 360, dtype=torch.float64)
    labels = torch.randn(17, dtype=torch.float64)
    for step in range(3):
        for network, optimizer in ((reference, left), (candidate, right)):
            torch.manual_seed(100 + step)
            optimizer.zero_grad(set_to_none=True)
            torch.square(network(values) - labels).mean().backward()
            optimizer.step()
        for actual, expected in zip(candidate.parameters(), reference.parameters(), strict=True):
            torch.testing.assert_close(actual, expected, rtol=1e-9, atol=1e-11)
    candidate.eval()
    reference.eval()
    restored = TcnNetwork(auxiliary, 0.1)
    restored.load_tensors(candidate.tensors())
    restored.eval()
    with torch.inference_mode():
        actual = candidate(values)
        torch.testing.assert_close(actual, reference(values), rtol=1e-9, atol=1e-11)
        torch.testing.assert_close(restored(values), actual, rtol=0, atol=0)
