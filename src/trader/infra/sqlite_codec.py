"""Strict scalar codecs for compact SQLite dates, instants and SHA-256 values."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_EPOCH_DATE = date(1970, 1, 1)
_EPOCH_ORDINAL = _EPOCH_DATE.toordinal()
_EPOCH_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def EncodeDate(value: date) -> int:
    if type(value) is not date:
        raise ValueError("sqlite_date_invalid")
    return value.toordinal() - _EPOCH_ORDINAL


def DecodeDate(value: object) -> date:
    if type(value) is not int:
        raise ValueError("sqlite_date_invalid")
    try:
        return date.fromordinal(value + _EPOCH_ORDINAL)
    except (ValueError, OverflowError) as exc:
        raise ValueError("sqlite_date_invalid") from exc


def EncodeTime(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sqlite_time_invalid")
    delta = value.astimezone(timezone.utc) - _EPOCH_TIME
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


def DecodeTime(value: object) -> datetime:
    if type(value) is not int:
        raise ValueError("sqlite_time_invalid")
    try:
        return (_EPOCH_TIME + timedelta(microseconds=value)).astimezone(_SHANGHAI)
    except OverflowError as exc:
        raise ValueError("sqlite_time_invalid") from exc


def EncodeHash(value: str) -> bytes:
    if len(value) != 64:
        raise ValueError("sqlite_hash_invalid")
    try:
        digest = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("sqlite_hash_invalid") from exc
    if digest.hex() != value:
        raise ValueError("sqlite_hash_invalid")
    return digest


def DecodeHash(value: object) -> str:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("sqlite_hash_invalid")
    return value.hex()


__all__ = ["EncodeDate", "DecodeDate", "EncodeTime", "DecodeTime", "EncodeHash", "DecodeHash"]
