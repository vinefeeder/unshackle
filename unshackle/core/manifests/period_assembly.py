"""Period-preserving assembly helpers for multi-period MPEG-DASH.

The DASH downloader supplies media fragment files and one initialization segment
for each period. This module deliberately does not acquire licences or interpret
service-specific advertising metadata. Its output is a sequence of standalone
per-period fragment streams for the existing DRM and muxing stages.

Do NOT concatenate encrypted periods before checking their per-period DRM state.
"""

from __future__ import annotations

import subprocess
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class PeriodSpan:
    """Half-open slice of the flattened media segment array."""

    period_index: int
    first_segment: int
    last_segment: int
    init_data: bytes | None

    @property
    def count(self) -> int:
        return self.last_segment - self.first_segment


def add_span(
    spans: list[PeriodSpan],
    period_index: int,
    segment_count: int,
    init_data: bytes | None,
) -> None:
    """Record a contiguous period while flattening the segment references."""
    if segment_count < 0:
        raise ValueError("Segment count cannot be negative")
    first = spans[-1].last_segment if spans else 0
    if segment_count:
        spans.append(PeriodSpan(period_index, first, first + segment_count, init_data))


def validate_spans(spans: Sequence[PeriodSpan], segment_count: int) -> None:
    """Reject gaps, overlaps or incomplete period-boundary metadata."""
    next_index = 0
    for span in spans:
        if span.first_segment != next_index or span.count <= 0:
            raise ValueError(f"Non-contiguous or empty DASH period span: {span!r}")
        next_index = span.last_segment
    if next_index != segment_count:
        raise ValueError(f"Period spans cover {next_index} of {segment_count} segments")


def split_segment_list(segments: Sequence[T], spans: Sequence[PeriodSpan]) -> list[list[T]]:
    """Partition downloaded media segments without changing their order."""
    validate_spans(spans, len(segments))
    return [list(segments[s.first_segment:s.last_segment]) for s in spans]


def assemble_parts(
    segments: Sequence[Path],
    spans: Sequence[PeriodSpan],
    directory: Path,
    *,
    transform: Callable[[Path, PeriodSpan], Path] | None = None,
) -> list[Path]:
    """Build one initialized fMP4 per period; optionally transform each finished part.

    Caller guarantees media fragments belong to the period shown in spans.
    A DRM transform, if needed, must process the part before concatenation.
    """
    validate_spans(spans, len(segments))
    directory.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for index, span in enumerate(spans):
        if span.init_data is None:
            raise ValueError(f"No initialization segment for period {span.period_index}")
        path = directory / f"period_{index:04d}.mp4"
        with path.open("wb") as output:
            output.write(span.init_data)
            for fragment in segments[span.first_segment:span.last_segment]:
                with fragment.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        results.append(transform(path, span) if transform else path)
    return results


def remux_parts(
    parts: Sequence[Path],
    directory: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[Path]:
    """Copy-remux each *clear* period independently before concatenating.

    Standalone MP4 muxing rebuilds timing/index metadata and gives the concat
    demuxer independent files to rebase. It never re-encodes video or audio.
    """
    directory.mkdir(parents=True, exist_ok=True)
    remuxed = []
    for index, part in enumerate(parts):
        target = directory / f"normalized_{index:04d}.mp4"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
                "-fflags", "+genpts", "-i", str(part),
                "-map", "0", "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart", str(target),
            ],
            check=True,
        )
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError(f"FFmpeg produced no data for period {index}")
        remuxed.append(target)
    return remuxed


def concatenate_parts(parts: Sequence[Path], destination: Path, *, ffmpeg: str = "ffmpeg") -> None:
    """Concatenate independently remuxed, compatible clear streams without re-encoding.

    Use a real concat script on disk: an input supplied via pipe:0 causes
    FFmpeg to resolve absolute file entries as pipe:/path and fail with 234.
    This only rebases *between* independently remuxed periods; it cannot
    fix corrupt timestamps within an individual part.
    """
    if not parts:
        raise ValueError("No parts available for concatenation")
    for path in parts:
        if not path.is_file():
            raise FileNotFoundError(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    script_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".ffconcat",
            prefix="dash_parts_", dir=destination.parent, delete=False,
        ) as script:
            script_path = Path(script.name)
            script.write("ffconcat version 1.0\n")
            for part in parts:
                # FFmpeg concat demuxer single-quote escaping:
                # file '/path/that/has'\\''apostrophe.mp4'
                quoted = str(part.resolve()).replace("'", "'\\''")
                script.write(f"file '{quoted}'\n")

        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "warning",
             "-f", "concat", "-safe", "0", "-i", str(script_path),
             "-map", "0", "-c", "copy", "-y", str(destination)],
            check=True,
        )
    finally:
        if script_path is not None:
            script_path.unlink(missing_ok=True)
