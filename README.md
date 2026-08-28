# vidsmasharr

Space-reclaiming media optimiser for a Synology DS1019+ running Plex.

Inspired by [Unmanic](https://github.com/Unmanic/unmanic), but with the
priorities inverted. Unmanic works through a library converting things. On a
15TB library and a Celeron J3455 that never finishes — a full pass is roughly
2,000 hours of encoding. So this tool instead:

1. **Finds free space that costs no CPU first** — duplicate copies, redundant
   audio tracks. On a large library this is usually the biggest single win, and
   it is available in hours rather than months.
2. **Then encodes, ranked by GB saved per CPU-hour** — so the first month of
   work reclaims far more than working alphabetically would.

## Hardware reality

The DS1019+ has an Intel Celeron J3455 (Apollo Lake). This drives every design
decision in the project:

| Capability | Status |
|---|---|
| QuickSync H.264 encode/decode | Yes |
| QuickSync HEVC **8-bit** encode/decode | Yes |
| QuickSync HEVC **10-bit encode** | **No — decode only** |
| Software x265 at 1080p | ~3–6 fps (3–6 hours per episode at 100% CPU) |

The consequence that matters most: **HDR content can never touch the hardware
encoder.** It would come out 8-bit SDR and the HDR grade would be gone
permanently. `app/scan/probe.py` therefore treats anything it cannot *prove* is
8-bit SDR as protected — PQ/HLG transfer, Dolby Vision side data, BT.2020
primaries, any bit depth above 8, or an undeterminable stream. A false positive
costs some savings on one file; a false negative destroys it.

## Status

**Phases 0 and 1 are complete.** Phases 2–4 are not built yet.

| Phase | What | State |
|---|---|---|
| 0 | Capability detection, benchmark, quality ladder | Done |
| 1 | Scan, identify via Plex/*arr, duplicate report + UI | Done |
| 2 | Decision engine, planner, dry-run plan | Not started |
| 3 | Encode pipeline, verification, atomic swap, scheduler | Not started |
| 4 | *arr profile guard, estimator calibration, x265 keepers | Not started |

Nothing in the current code modifies your media library. Phases 0 and 1 only
read files, and write to a scratch directory and the SQLite database.

## Phase 0: run this first

Nothing downstream is designed correctly until these numbers exist. The
benchmark answers, on your actual box:

- does hardware encoding work at all here (kernel, permissions, ffmpeg build)?
- `hevc_vaapi` or `hevc_qsv` — which is faster, and which even initialises?
- is a full hardware decode pipeline faster than software decode + hardware
  encode, or does it fail on your sources?
- what QP actually lands on VMAF 95 (movies) and 92 (TV) for *your* content?
- how many months would a full library pass really take?

### 1. Find your render group id

On the NAS over SSH:

```sh
stat -c '%g' /dev/dri/renderD128
```

Put the result in `RENDER_GID` in `docker/docker-compose.yml` (Synology
commonly uses 937). Without it the container can see `/dev/dri` but not open
it, and every hardware encode fails.

### 2. Configure

```sh
cp config/config.example.yaml config/config.yaml
```

Edit the library paths, and the Plex/Sonarr/Radarr URLs and keys. Paths are as
the *container* sees them — check them against the volume mounts.

### 3. Capability report

```sh
docker compose run --rm vidsmasharr bench.capability
```

Every encoder is verified by really encoding a one-second clip, not by reading
a feature list. If `preferred hardware encoder` comes back `NONE`, stop and fix
that — software-only is not viable at this library size.

### 4. Benchmark and calibrate

```sh
docker compose run --rm vidsmasharr bench --libraries /media/tv
```

Takes roughly an evening. Writes `config/profiles.yaml` (the quality ladder
production will use) plus a full JSON report.

Useful flags: `--dry-run` lists the matrix without running it, `--sources N`
changes how many library files are sampled, `--include-software` adds an x265
sweep (slow), `--keep-clips` retains the extracted samples for a re-run with
`--reuse-clips`.

### 5. Verify direct play — do not skip this

The benchmark says nothing about whether your TVs will *play* the output. If a
TV can't decode HEVC, Plex transcodes on the J3455 at playback time and the
whole plan backfires.

1. Encode 2–3 real files with the ladder settings.
2. Put them in the Plex library and play one on **each** TV.
3. In Plex's *Now Playing*, confirm each says **Direct Play**, not Transcode.

If either TV transcodes, revisit the codec choice here — before encoding
thousands of files.

## Phase 1: index the library

Phase 1 answers *what have you actually got, and what have you got twice?* It
reads; it never writes to your media.

```sh
docker compose -f docker/docker-compose.yml run --rm vidsmasharr app phase1
```

That runs three steps, each also available on its own:

| Step | Command | What it does |
|---|---|---|
| Scan | `app scan` | Walks the libraries, then ffprobes anything new or changed |
| Identify | `app identify` | Resolves files to titles via Plex, then Sonarr/Radarr, then filenames |
| Duplicates | `app duplicates` | Groups copies of the same thing and ranks which to keep |

`app status` prints what the database currently knows. The web UI is on
**http://<nas>:8330** once the container is up.

### Notes that matter

- **The first scan is the slow one.** Probing is largest-file-first, so an
  interrupted scan is still useful; `--probe-limit N` caps a run. Rescans only
  probe files whose size or mtime moved.
- **A library that walks up empty is treated as a mount failure, not a
  deletion.** If a root loses more than half its known files the scanner
  refuses to mark anything missing and tells you why. Nothing is ever deleted
  from the database either — files that disappear are flagged `missing`.
- **Identity confidence is recorded.** Plex resolves at 1.0, the *arrs at 0.95,
  filenames below that. The UI shows which source matched each file.
- **The duplicate report is report-only**, by design. Choosing a keeper or
  dismissing a group records *your* decision and survives future rebuilds; it
  does not move, delete or queue anything.
- **Ambiguous groups are flagged rather than ranked.** Copies whose runtimes
  differ (likely different cuts), HDR-versus-resolution trade-offs, and copies
  too similar to separate are all marked *needs a human*.

## Safety model

Enforced across the project, and the parts that exist already respect it:

1. Encoding happens in `/scratch`, never in place.
2. An original is deleted only after verification passes **and** the atomic
   rename succeeds.
3. A hard free-space floor blocks jobs that would take the volume below it.
4. Verification failure keeps the original and quarantines the output.
5. **Dedupe never deletes anything.** It produces a report; you decide.
6. `delete_original_on_success` and `dry_run` ship in their safe positions.
   Leave them there until you've watched a first batch on both TVs.

## Development

```sh
python -m venv .venv
./.venv/Scripts/python -m pip install -e ".[dev]"
./.venv/Scripts/python -m pytest tests/ -q
```

The test suite covers the two things most expensive to get wrong: HDR
detection (`tests/test_probe_hdr.py`) and quality-ladder interpolation
(`tests/test_ladder.py`).

You can exercise the whole Phase 0 pipeline on a workstation without any
Intel GPU:

```sh
python -m bench --libraries /path/to/media --allow-software-only \
    --include-software --sources 1 --clip-seconds 6
```

## Layout

```
app/
  cli.py               `app scan|identify|duplicates|phase1|status|serve`
  config.py            YAML + env config, safe defaults
  db.py                SQLite schema and migrations
  scan/probe.py        ffprobe -> normalised facts, HDR detection
  scan/walker.py       library traversal, sampling
  scan/index.py        incremental sync: disk -> rows -> probe facts
  identity/plex.py     the Plex library DB (snapshotted, never opened live)
  identity/arr.py      Sonarr / Radarr read-only clients
  identity/filename.py fallback parsing, always below full confidence
  identity/resolve.py  merge the sources into title / file_title
  dedupe/groups.py     duplicate grouping and keeper ranking (report only)
  web/                 FastAPI + Jinja2 UI: overview, duplicates, library
  work/ffmpeg_cmd.py   encode + VMAF command construction
bench/
  capability.py        what this box can actually do
  runner.py            clip extraction, encode matrix, VMAF scoring
  ladder.py            interpolate settings that hit the VMAF targets
  __main__.py          the five-step Phase 0 run
docker/                Dockerfile (jellyfin-ffmpeg) + compose for DSM
```
