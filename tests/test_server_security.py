from unittest.mock import patch

import pytest

from stock_analyzer import config
from stock_analyzer.server_security import is_loopback_bind_host, validate_server_bind


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.10.20.30", "::1", "[::1]"])
def test_loopback_bind_hosts_are_allowed(host):
    assert is_loopback_bind_host(host) is True
    validate_server_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "dashboard.local"])
def test_non_loopback_bind_hosts_are_rejected_by_default(host):
    with (
        patch.object(config, "SERVER_ALLOW_INSECURE_NON_LOOPBACK", False),
        pytest.raises(RuntimeError, match="refusing unauthenticated non-loopback bind"),
    ):
        validate_server_bind(host)


def test_non_loopback_bind_requires_explicit_insecure_acknowledgement():
    with patch.object(config, "SERVER_ALLOW_INSECURE_NON_LOOPBACK", True):
        validate_server_bind("0.0.0.0")
