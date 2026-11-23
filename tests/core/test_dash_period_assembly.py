"""Tests for DASH period-boundary assembly (no external downloads required)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from unshackle.core.manifests.period_assembly import (
    PeriodSpan,
    add_span,
    assemble_parts,
    concatenate_parts,
    remux_parts,
    split_segment_list,
    validate_spans,
)


def test_segment_spans_preserve_boundaries():
    spans = []
    add_span(spans, 0, 2, b"init-A")
    add_span(spans, 8, 3, b"init-B")
    assert split_segment_list(["a", "b", "c", "d", "e"], spans) == [
        ["a", "b"], ["c", "d", "e"],
    ]
    assert [(s.first_segment, s.last_segment) for s in spans] == [(0, 2), (2, 5)]


def test_incomplete_span_rejected():
    with pytest.raises(ValueError, match="cover"):
        validate_spans([PeriodSpan(0, 0, 2, b"init")], 3)


def test_assembly_writes_one_init_per_period(tmp_path: Path):
    segments = []
    for index, value in enumerate((b"A", b"B", b"C")):
        path = tmp_path / f"{index}.m4s"
        path.write_bytes(value)
        segments.append(path)
    spans = [
        PeriodSpan(0, 0, 2, b"first-init"),
        PeriodSpan(2, 2, 3, b"next-init"),
    ]
    parts = assemble_parts(segments, spans, tmp_path / "parts")
    assert [p.read_bytes() for p in parts] == [b"first-initAB", b"next-initC"]


def test_missing_init_fails_closed(tmp_path: Path):
    segment = tmp_path / "one.m4s"
    segment.write_bytes(b"segment")
    with pytest.raises(ValueError, match="initialization"):
        assemble_parts([segment], [PeriodSpan(0, 0, 1, None)], tmp_path / "parts")


def test_concat_calls_stream_copy(tmp_path: Path):
    first = tmp_path / "part0.mp4"
    second = tmp_path / "part1.mp4"
    first.touch()
    second.touch()
    destination = tmp_path / "output.mp4"
    with patch("unshackle.core.manifests.period_assembly.subprocess.run") as run:
        concatenate_parts([first, second], destination)
    command = run.call_args.args[0]
    assert command[command.index("-c") + 1] == "copy"
    assert run.call_args.kwargs["check"] is True
    assert str(first.resolve()) in run.call_args.kwargs["input"]
    assert str(second.resolve()) in run.call_args.kwargs["input"]


def test_remux_uses_copy_without_reencoding(tmp_path: Path):
    part = tmp_path / "part.mp4"
    part.write_bytes(b"fake content")
    normalized = tmp_path / "normalized"

    def fake_run(command, **kwargs):
        assert command[command.index("-c") + 1] == "copy"
        assert "-avoid_negative_ts" in command
        Path(command[-1]).write_bytes(b"normalized")

    with patch("unshackle.core.manifests.period_assembly.subprocess.run", side_effect=fake_run) as run:
        result = remux_parts([part], normalized)
    assert result[0].read_bytes() == b"normalized"
    assert run.call_count == 1


def test_remux_rejects_empty_output(tmp_path: Path):
    part = tmp_path / "part.mp4"
    part.touch()

    with patch("unshackle.core.manifests.period_assembly.subprocess.run"):
        with pytest.raises(ValueError, match="no data"):
            remux_parts([part], tmp_path / "normalized")
