# Selecting and Assembling MPEG-DASH Periods

An MPEG-DASH manifest (`.mpd`) can contain multiple `<Period>` elements.
Each period describes a time interval with its own representations, segments,
initialization data, and possibly encryption metadata. A platform can use
period boundaries for programme sections, adverts, or other presentation
changes.

This guide is for **service authors** who need to keep selected periods from
a multi-period DASH manifest, and for core contributors working on the
period-aware downloader.

!!! note "Division of responsibility"
    **The service determines which periods belong in the output.** It can use
    an API response, a playout schedule, VMAP, SSAI metadata, or other reliable
    service-specific information.

    **Unshackle core validates the supplied timing plan and processes the
    selected DASH periods.** It does not interpret `clip`, `ad`, VMAP, or any
    platform's JSON field names. The core does not automatically identify ads
    simply because a manifest contains multiple periods.

## The underlying problem

For example, suppose the MPD contains:

| MPD index (zero-based) | Start | Duration | Keep? |
|---|---|---|---|
| 0 | 0 s | 10 s | Yes |
| 1 | 10 s | 5 s | No |
| 2 | 15 s | 12 s | Yes |

This describes 27 seconds of presentation, but only 22 seconds of retained
content. The second kept period starts at 15 seconds in the original MPD; in
the output it must follow the first kept period at 10 seconds.

Simply appending the media fragments from both kept periods, with only the
first period's initialization segment, may produce invalid timestamps
(non-monotonic DTS/PTS) or video that stops displaying at the period boundary
while audio continues. **Period selection and period-aware assembly are
separate problems.**

## 1. Obtain selection metadata in the service

A service typically fetches the manifest and its corresponding presentation
or playout metadata during `get_tracks()`. The metadata format is
service-specific. Convert it to the generic model:

- `TimelineInterval(start_ms, duration_ms, keep)` represents one interval
  on the **original presentation timeline**, in integer milliseconds.
- `keep=True` retains the interval; `keep=False` excludes it.
- Entries must be ordered, and should describe **every MPD period**, including
  those to be excluded.
- Derive the entries from the service's own trustworthy timing and content
  information. Do not assume all even-numbered periods, or all periods with a
  particular URL substring, are programme content.

The following *illustrative* adapter assumes a service returns three
time-coded entries already converted to milliseconds:

```python
from unshackle.core.manifests.period_plan import TimelineInterval

# This data and its "programme" field belong to the example service, NOT core.
schedule = [
    {"start_ms": 0, "duration_ms": 10_000, "programme": True},
    {"start_ms": 10_000, "duration_ms": 5_000, "programme": False},
    {"start_ms": 15_000, "duration_ms": 12_000, "programme": True},
]

intervals = [
    TimelineInterval(
        start_ms=item["start_ms"],
        duration_ms=item["duration_ms"],
        keep=item["programme"],
    )
    for item in schedule
]
```

!!! warning "Use matching session data"
    Dynamically inserted advertising can change between playback sessions.
    Fetch the MPD and the service's playout information for the **same
    presentation/session**. Never apply an old interval list blindly to a new
    MPD.

## 2. Build and validate a generic `PeriodPlan`

`PeriodPlan.build(manifest, intervals, tolerance_ms=50)` matches the supplied
intervals, in order, against the MPD's `<Period>` elements.

```python
from unshackle.core.manifests import DASH
from unshackle.core.manifests.period_plan import PeriodPlan

# manifest_text and manifest_url were obtained by your service.
dash = DASH.from_text(manifest_text, manifest_url)
plan = PeriodPlan.build(dash.manifest, intervals)
```

The builder requires matching counts and checks each MPD period's start and
explicit duration against the corresponding interval, with a default
**50-millisecond tolerance**. The supported MPD duration syntax is
`PT...` with hours, minutes and seconds to millisecond precision.
Invalid, incomplete, inconsistent or entirely excluded plans raise
`ValueError` rather than silently selecting the wrong media.

`plan.kept` contains `PlannedPeriod` records, including the original
zero-based `index`, MPD `period_id`, original `start_ms`,
`duration_ms`, and cumulative `programme_start_ms`.

`plan.discarded` holds the indices excluded from the output, and
`plan.count` is the number of original MPD periods.

For the example above, the retained indices are `(0, 2)`, and their
cumulative output start times are 0 ms and 10,000 ms.

!!! important "Validation is not a semantic classifier"
    The core can check that timings match, but **cannot decide whether the
    service's `keep` values are correct**. That remains the service author's
    responsibility. Check the episode or stream identity in your service as
    well as any timing or session identity.

## 3. Supply the plan when parsing tracks

Pass the plan to the same `DASH` instance whose parsed manifest was used
to create it:

```python
tracks = dash.to_tracks(
    language=title.language,
    period_plan=plan,
)
return tracks
```

This is the recommended interface when you need **both** period filtering and
correct per-period assembly. It performs two actions together:

1. Excludes the discarded MPD periods when enumerating and downloading tracks.
2. Enables `period_aware` processing for the returned video and audio tracks.

Do not also pass `period_filter`; the two arguments are mutually exclusive.

### How this differs from `period_filter`

`DASH.to_tracks(period_filter=callable)` remains available for existing
services. The callback returns `True` for an MPD `<Period>` element
that should be excluded:

```python
tracks = dash.to_tracks(
    language=title.language,
    period_filter=lambda period: period.get("id") == "exclude-me",
)
```

That **legacy filter alone does not enable independent period-aware assembly**.
Use `period_plan=plan` when the selected media must be joined across
discontinuous boundaries. Existing automatic exclusions for known
non-content/caching periods are still applied.

## 4. What the generic downloader does

For tracks using `period_plan`, the DASH downloader:

1. Resolves the matching representation across retained content periods.
2. Downloads the fragments belonging to each retained period.
3. Records boundaries as `PeriodSpan` entries and preserves each period's
   initialization segment.
4. Builds one initialized fragmented MP4 per retained period.
5. Decrypts each part when required.
6. Runs FFmpeg with `-c copy` to remux each part independently, then
   concatenates the remuxed parts through the FFmpeg concat demuxer.

This avoids **re-encoding** media. The period-aware path requires FFmpeg.

!!! warning "Current limitations"
    - A manifest can contain incompatible representations or track layouts
      across periods. Concatenation is not guaranteed for every MPD.
    - The downloader rejects detected changes in content KID between retained
      encrypted periods; per-period key-rotation licensing needs separate work.
    - A copy-remux cannot repair arbitrary damage or broken timing *within*
      an individual period.
    - The service must provide a complete, matching interval plan. This API
      does not attempt partial-period trimming or infer programme content.

## 5. Tests for a new service adapter

Keep tests of service-specific schedule interpretation **with the service**.
Useful cases include correctly ordered content and non-content entries,
changed advertising durations, stale MPD/session pairs, unexpected entry
types, and wrong programme/episode identifiers.

The core has separate tests for period timing/selection and assembly in
`tests/core/test_dash_period_plan.py` and
`tests/core/test_dash_period_assembly.py`. A service-specific JSON/VMAP
schema or API URL does not belong in those generic tests.

For the surrounding service lifecycle, see
[Creating a Service](creating-a-service.md) and
[Service Architecture](service-architecture.md).
