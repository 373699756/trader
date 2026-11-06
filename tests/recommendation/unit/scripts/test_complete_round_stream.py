from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scripts.recommendation.web.complete_round_stream import EventStreamDecoder, EventStreamFrame


def test_split_utf8_and_crlf_preserve_one_complete_frame():
    decoder = EventStreamDecoder()
    payload = "id: 7\r\nevent: batch\r\ndata: 第一\r\ndata: 第二\r\n\r\n".encode()
    frames = tuple(frame for value in payload for frame in decoder.feed(bytes((value,))))

    assert frames == (EventStreamFrame("batch", 7, "第一\n第二"),)
    assert decoder.buffered_bytes == 0
    with pytest.raises(FrozenInstanceError):
        frames[0].event_id = 8


def test_comments_empty_frames_and_unknown_fields_do_not_dispatch_messages():
    decoder = EventStreamDecoder()

    assert decoder.feed(b": connected\n\n: heartbeat\r\n\r\n\nretry: 1000\n\n") == ()
    assert decoder.buffered_bytes == 0
    assert decoder.feed(b"data: one\nignored: value\ndata: two\n\n") == (
        EventStreamFrame("message", None, "one\ntwo"),
    )
    assert decoder.feed(b"data:\n\n") == (EventStreamFrame("message", None, ""),)


def test_lone_carriage_returns_and_split_line_endings_do_not_duplicate_a_frame():
    decoder = EventStreamDecoder()

    assert decoder.feed(b"id: 1\rdata: first\r\r") == (EventStreamFrame("message", 1, "first"),)
    assert decoder.feed(b"\nid: 2\r") == ()
    assert decoder.feed(b"\ndata: second\r\n\r") == (EventStreamFrame("message", 2, "second"),)
    assert decoder.feed(b"\n") == ()
    assert decoder.buffered_bytes == 0


def test_each_complete_frame_releases_its_budget_even_in_a_large_chunk():
    decoder = EventStreamDecoder(max_frame_bytes=64)
    payload = b"id: 1\ndata: bounded\n\n"

    frames = decoder.feed(payload * 10_000)

    assert len(frames) == 10_000
    assert all(frame == EventStreamFrame("message", 1, "bounded") for frame in frames)
    assert decoder.buffered_bytes == 0
    assert decoder.feed(b"data: next\n\n") == (EventStreamFrame("message", None, "next"),)


@pytest.mark.parametrize("payload", (b"data: " + b"x" * 32, b"data: first\ndata: second\ndata: third\n\n"))
def test_incomplete_lines_and_complete_multiline_frames_share_one_limit(payload):
    decoder = EventStreamDecoder(max_frame_bytes=32)

    with pytest.raises(ValueError, match="^event_stream_frame_too_large$"):
        decoder.feed(payload)
    assert decoder.buffered_bytes == 0
    with pytest.raises(ValueError, match="^event_stream_decoder_failed$"):
        decoder.feed(b"data: cannot-resume\n\n")


def test_incremental_limit_counts_previously_parsed_data_and_partial_next_line():
    decoder = EventStreamDecoder(max_frame_bytes=24)
    assert decoder.feed(b"data: first\n") == ()

    with pytest.raises(ValueError, match="^event_stream_frame_too_large$"):
        decoder.feed(b"data: second-long")


def test_realistic_single_line_payload_is_not_limited_to_transport_chunk_size():
    decoder = EventStreamDecoder()
    value = "x" * 524_288

    frames = decoder.feed(("id: 2\nevent: batch\ndata: " + value + "\n\n").encode())

    assert frames == (EventStreamFrame("batch", 2, value),)
    assert decoder.buffered_bytes == 0


@pytest.mark.parametrize("separator", (b"\n", b"\r", b"\r\n"))
@pytest.mark.parametrize("fragmented", (False, True))
def test_logical_frame_limit_is_independent_of_newline_fragmentation(separator, fragmented):
    payload = separator.join((b"id: 2", b"data: value", b"", b""))
    decoder = EventStreamDecoder(max_frame_bytes=19)
    chunks = tuple(bytes((value,)) for value in payload) if fragmented else (payload,)

    assert tuple(frame for chunk in chunks for frame in decoder.feed(chunk)) == (
        EventStreamFrame("message", 2, "value"),
    )
    assert decoder.buffered_bytes == 0


@pytest.mark.parametrize("field", (
    b"id: -1", b"id:", b"id: 1 2", b"id: 99999999999999999999", b"id: 9223372036854775808",
    "id: １２".encode(),
))
def test_invalid_event_ids_are_rejected_without_payload_text(field):
    decoder = EventStreamDecoder()

    with pytest.raises(ValueError, match="^event_stream_id_invalid$"):
        decoder.feed(field + b"\ndata: hidden-payload\n\n")


def test_duplicate_id_in_one_frame_is_not_silently_replaced():
    decoder = EventStreamDecoder()

    with pytest.raises(ValueError, match="^event_stream_duplicate_id$"):
        decoder.feed(b"id: 1\nid: 2\ndata: hidden-payload\n\n")


@pytest.mark.parametrize("payload,reason", (
    (b"data: \xff\n\n", "event_stream_utf8_invalid"),
    (b"id: 1\x00\ndata: hidden\n\n", "event_stream_nul_invalid"),
    (b"event: \x01\ndata: hidden\n\n", "event_stream_event_type_invalid"),
))
def test_invalid_frame_text_uses_a_bounded_reason(payload, reason):
    with pytest.raises(ValueError, match="^" + reason + "$"):
        EventStreamDecoder().feed(payload)


@pytest.mark.parametrize("maximum", (0, -1, True, 1.5))
def test_frame_limit_must_be_a_positive_integer(maximum):
    with pytest.raises(ValueError, match="^event_stream_frame_limit_invalid$"):
        EventStreamDecoder(max_frame_bytes=maximum)
