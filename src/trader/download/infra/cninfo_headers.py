"""CNINFO's published SDK timestamp header, without a JavaScript runtime."""

from __future__ import annotations

import base64
import math
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# AKShare's bundled cninfo.js/getResCode1 uses this public vendor constant for
# both key and IV. It is part of the supplier protocol, not a user credential.
_VENDOR_VALUE = b"1234567887654321"


def CninfoIndustryHeaders(observed_at: datetime) -> tuple[tuple[str, str], ...]:
    """Encode the injected aware clock exactly as the supplier's getResCode1."""
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise ValueError("cninfo_industry_clock_invalid")
    seconds = str(math.floor(observed_at.timestamp())).encode("ascii")
    cipher = AES.new(_VENDOR_VALUE, AES.MODE_CBC, iv=_VENDOR_VALUE)
    enckey = base64.b64encode(cipher.encrypt(pad(seconds, AES.block_size))).decode("ascii")
    return (
        ("Accept", "application/json"),
        ("Accept-Encoding", "identity"),
        ("Accept-Enckey", enckey),
        ("Origin", "https://webapi.cninfo.com.cn"),
        ("Referer", "https://webapi.cninfo.com.cn/"),
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/93.0.4577.63 Safari/537.36"),
        ("X-Requested-With", "XMLHttpRequest"),
    )


__all__ = ["CninfoIndustryHeaders"]
