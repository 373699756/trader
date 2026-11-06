from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trader.download.infra.cninfo_headers import CninfoIndustryHeaders

_OBSERVED_AT = datetime(2026, 11, 6, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
# Computed once from the installed AKShare cninfo.js/getResCode1 with its Date
# set to 1793948400000 milliseconds. Tests do not import or execute that SDK.
_SDK_REFERENCE = "QQjiwvT+zme1nvrgzWA9aQ=="


def test_header_matches_the_vendor_sdk_reference_vector():
    headers = CninfoIndustryHeaders(_OBSERVED_AT)
    assert dict(headers)["Accept-Enckey"] == _SDK_REFERENCE
    assert len(dict(headers)) == len(headers)


def test_equal_instants_in_other_timezones_produce_the_same_protocol_header():
    headers = CninfoIndustryHeaders(_OBSERVED_AT)
    assert CninfoIndustryHeaders(_OBSERVED_AT.astimezone(timezone.utc)) == headers
    assert CninfoIndustryHeaders(_OBSERVED_AT.astimezone(timezone(timedelta(hours=-5)))) == headers


def test_subsecond_clock_precision_uses_the_sdk_floor_to_whole_seconds():
    assert CninfoIndustryHeaders(_OBSERVED_AT.replace(microsecond=999999)) == CninfoIndustryHeaders(_OBSERVED_AT)
    assert CninfoIndustryHeaders(_OBSERVED_AT + timedelta(seconds=1)) != CninfoIndustryHeaders(_OBSERVED_AT)


def test_naive_clock_is_rejected_before_generating_a_protocol_header():
    with pytest.raises(ValueError, match="clock_invalid"):
        CninfoIndustryHeaders(_OBSERVED_AT.replace(tzinfo=None))


def test_headers_identify_the_actual_supplier_origin():
    headers = dict(CninfoIndustryHeaders(_OBSERVED_AT))
    assert headers["Origin"] == "https://webapi.cninfo.com.cn"
    assert headers["Referer"] == "https://webapi.cninfo.com.cn/"
    assert headers["Accept"] == "application/json"
    assert headers["Accept-Encoding"] == "identity"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
