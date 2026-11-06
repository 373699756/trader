"""Bounded incremental framing for the diagnostic SSE connection."""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_MAX_FRAME_BYTES = 1_048_576
_MAX_EVENT_ID = (1 << 63) - 1


@dataclass(frozen=True)
class EventStreamFrame:
    event_type: str
    event_id: int | None
    data: str


class EventStreamDecoder:
    """Decode complete frames without retaining previous events or partial history."""

    def __init__(self, *, max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES) -> None:
        if type(max_frame_bytes) is not int or max_frame_bytes < 1:
            raise ValueError("event_stream_frame_limit_invalid")
        self._maximum = max_frame_bytes
        self._buffer = bytearray()
        self._scan_offset = 0
        self._frame_bytes = 0
        self._event_type = "message"
        self._event_id: int | None = None
        self._id_seen = False
        self._data_lines: list[str] = []
        self._skip_lf = False
        self._failed = False

    @property
    def buffered_bytes(self) -> int:
        return self._frame_bytes + len(self._buffer)

    def feed(self, chunk: bytes) -> tuple[EventStreamFrame, ...]:
        if self._failed:
            raise ValueError("event_stream_decoder_failed")
        frames: list[EventStreamFrame] = []
        try:
            # Keep an incoming multi-frame chunk from becoming a second copy of
            # its entire body while enforcing the per-frame bound incrementally.
            for offset in range(0, len(chunk), 65_536):
                self._buffer.extend(chunk[offset:offset + 65_536])
                self._drain(frames)
                if self.buffered_bytes > self._maximum:
                    raise ValueError("event_stream_frame_too_large")
        except ValueError:
            self._failed = True
            self._buffer.clear()
            self._scan_offset = 0
            self._reset_frame()
            raise
        return tuple(frames)

    def _drain(self, frames: list[EventStreamFrame]) -> None:
        consumed = 0
        size = len(self._buffer)
        if self._skip_lf and size:
            consumed = int(self._buffer[0] == 10)
            self._skip_lf = False
        while consumed < size:
            scan_start = max(consumed, self._scan_offset)
            newline = self._buffer.find(b"\n", scan_start)
            carriage = self._buffer.find(b"\r", scan_start)
            delimiters = tuple(position for position in (newline, carriage) if position >= 0)
            if not delimiters:
                break
            position = min(delimiters)
            separator = 1
            if self._buffer[position] == 13:
                if position + 1 < size and self._buffer[position + 1] == 10:
                    separator = 2
                elif position + 1 == size:
                    self._skip_lf = True
            raw = bytes(self._buffer[consumed:position])
            # One logical line ending has one budget byte regardless of CRLF
            # fragmentation. The transport separately caps actual wire bytes.
            self._frame_bytes += len(raw) + 1
            if self._frame_bytes > self._maximum:
                raise ValueError("event_stream_frame_too_large")
            self._line(raw, frames)
            consumed = position + separator
        if consumed:
            del self._buffer[:consumed]
        self._scan_offset = size - consumed

    def _line(self, raw: bytes, frames: list[EventStreamFrame]) -> None:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("event_stream_utf8_invalid") from exc
        if "\x00" in line:
            raise ValueError("event_stream_nul_invalid")
        if not line:
            if self._data_lines:
                frames.append(EventStreamFrame(self._event_type, self._event_id, "\n".join(self._data_lines)))
            self._reset_frame()
            return
        if line.startswith(":"):
            return
        key, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if key == "data":
            self._data_lines.append(value)
        elif key == "event":
            if len(value) > 64 or any(ord(character) < 32 for character in value):
                raise ValueError("event_stream_event_type_invalid")
            self._event_type = value or "message"
        elif key == "id":
            if self._id_seen:
                raise ValueError("event_stream_duplicate_id")
            if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
                raise ValueError("event_stream_id_invalid")
            event_id = int(value)
            if event_id > _MAX_EVENT_ID:
                raise ValueError("event_stream_id_invalid")
            self._event_id = event_id
            self._id_seen = True

    def _reset_frame(self) -> None:
        self._frame_bytes = 0
        self._event_type = "message"
        self._event_id = None
        self._id_seen = False
        self._data_lines.clear()


__all__ = [
    "EventStreamDecoder",
    "EventStreamFrame",
]
