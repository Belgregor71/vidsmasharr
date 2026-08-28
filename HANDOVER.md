# Handover — sessions 1–4 (2026-08-27 → 28)

Read this first. It records what is *verified* on the real hardware versus what
is still assumed, so tomorrow doesn't re-litigate settled decisions or trust
unverified ones.

---

## NEXT SESSION: Phase 4 is built. Run it against the real box.

Every phase is now built. Nothing left is a coding task -- what remains is
running this against the real library and the real *arrs, in an order chosen so
that the cheap checks come before the expensive ones.

### 1. Read the movie calibration log first

A targeted movie calibration was still running when session 3 ended and its
result is **still unknown**:

```sh
bench --libraries /media/movies --content-class movie --sources 2 --qp-sweep 14 17 20 --keep-clips
```

Log at `/volume1/scratch/vidsmasharr/bench-movies.log`. Read the tail, then
fold it in with the original run rather than replacing it -- a movies-only run
on its own would drop every trustworthy TV rung:

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.ladder --all-runs --verbose
```

If the movie rungs still say "no tested setting reached VMAF 95", that is now a
decision to make rather than a run to repeat: either go lower (`--qp-sweep 10
12 14`) or lower `quality.movie_vmaf` deliberately. Do not let a rung pinned to
the sweep floor pass as calibrated -- its size ratio is not a measurement. See
the open question at the bottom of this file.

### 2. Verify direct play on both TVs

Still the cheapest check with the largest consequence, and still unverified.
Encode 2-3 real files at the ladder settings, put them in Plex, confirm each TV
says *Direct Play* rather than *Transcode*. If either transcodes, the CPU cost
moves to playback, the codec choice has to change, and the whole *arr guard
becomes moot. Do this before anything downstream of it.

### 3. Install the *arr guard before any bulk encoding

`app arr-guard` reads and reports; `--apply` is the only thing that writes.
Run it without `--apply` first and **read the sampled match rate** -- that
number decides whether the guard is worth applying at all:

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app arr-guard
```

Two things in that output need a human:

- **The match rate.** An *arr scores a file by its *release name*, and our
  re-encode keeps the original name, which usually says `x264`. The guard
  samples the *arr's own files and reports what fraction of the HEVC ones its
  regex would actually reach. If that is low, the guard protects little, and
  the fix -- `{MediaInfo VideoCodec}` in the naming format plus a library
  rename -- is the user's call. **This was designed for but never measured
  against the real instances; the sample is how we find out.**
- **An existing negative HEVC score.** A TRaSH-style `x265 (HD)` at -10000 is a
  common deliberate setting, and while it stands nothing the guard adds
  protects anything. `--neutralise` raises those to zero, opt-in.

Then `--apply`. Every write is recorded in `guard_change`, and
`app arr-guard --revert` puts it all back.

### 4. Run the plan and the first real encode

Unchanged from before -- see Next Steps further down. `app plan`, then
`app work --limit 1`, then `--limit 1 --execute` with deletion still off, then
watch the output on both TVs.

### 5. After a batch has run, calibrate

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app calibrate
```

Reports and changes nothing until `--apply`. It needs 8 outcomes per model
before it will produce a factor, so this is a step for after the first real
batch, not before it. Re-run `app plan` afterwards -- the point is the queue
order, and existing decisions keep the estimates they were written with.

### Do not re-litigate these while running it

- Anything not provably 8-bit SDR is never rewritten. No exception for "the
  *arr says it is fine".
- The guard's score is **positive**, and that is not a typo. Both *arrs compare
  a candidate release's custom-format score against the score of the file
  already on disk, so a high score on HEVC is what protects it. A negative
  score would make every H.264 release an upgrade over a night of work. The
  handover that specified Phase 4 said "negative"; it was wrong, and the code
  and its tests now pin the right sign.
- A dry-run diff before writing to Sonarr/Radarr is locked, and there is no
  flag to skip it.
- `app/identity/arr.py` stays read-only. The write verbs live in
  `app/guard/arr_guard.py`, in the one module with a reason for them.

---

## Session 4 (2026-08-28): Phase 4 built

```
app/guard/arr_guard.py   the *arr guard: plan, diff, apply, revert  [writes outside]
app/plan/calibrate.py    correct the estimator against jobs that really ran
app/plan/keepers.py      the hand-written list that gets software x265
app/db.py                + migration 7
app/cli.py               + `app arr-guard [--apply] [--revert] [--neutralise]`
                         + `app calibrate [--apply] [--reset] [--min-samples N]`
app/web/                 /activity now breaks accuracy out per estimator model
```

292 tests pass (Phase 4 added 60).

### A bug found on the way in, and it mattered

**Every successful job was erasing its own measurement.** `job.decision_id` and
`outcome.job_id` were `ON DELETE CASCADE`, and the worker deletes the decision
the moment it replaces an original -- so the delete cascaded through `job` into
`outcome` and took the row with it. The `est_saved_bytes` and `est_cpu_seconds`
columns recorded specifically for Phase 4 would have been gone by the time
Phase 4 came to read them, and the `/activity` page would have shown an empty
history after a successful night.

Migration 7 rebuilds both tables with `ON DELETE SET NULL`: the intent may go,
the record of what happened stays. It also adds `est_out_bytes`,
`estimate_basis`, `encoder`, `content_class` and `resolution` to `outcome`, so
calibration can be done per model rather than in aggregate.

`Database.migrate()` now runs with foreign keys off and a `foreign_key_check`
afterwards -- SQLite's own documented procedure. Without it, `DROP TABLE job`
performs an implicit DELETE and cascades into the rows the migration exists to
preserve.

### Phase 4 decisions worth not re-litigating

- **The guard's score is positive.** Explained above. There is a test named for
  it, because the wrong sign is the plausible-sounding one.
- **The guard predicts its own effect before writing.** It samples the *arr's
  files, finds the HEVC ones, and reports how many its regex would actually
  match. A guard that protects none of the files it exists for is worth
  knowing about before it is installed, and this was the one part of the design
  that could not be verified from a workstation.
- **It does not touch other people's custom formats without being asked.** A
  negative HEVC score defeats the guard entirely, so it is reported loudly, but
  `--neutralise` is opt-in: those scores are the user's own tuning.
- **It does not stop a genuine quality upgrade**, and says so. A profile
  climbing to Bluray-1080p replacing a WEB-DL HEVC file is the profile doing
  what it was told. Overriding that would be the guard deciding something that
  is not its to decide.
- **`app/identity/arr.py` stays read-only.** `ArrWriter` subclasses it rather
  than adding verbs to it. Phase 1 resolves identity through that class on
  every run and should stay incapable of changing anything; a test asserts
  `ArrClient` has no `post`, `put` or `delete`.
- **Calibration is per model, median, and clamped.** Per model because the
  ladder's bitrate and the policy target are wrong in different directions and
  an average hides both. Median because a concert film should not re-rank a
  year of work. Clamped to 0.25-4x because a factor outside that is a broken
  measurement, not a broken estimator -- and it warns rather than silently
  shipping it.
- **A corrected estimate is labelled `<model>+cal`.** The next calibration then
  measures the corrected model as its own population and its factor converges
  towards 1 instead of compounding on the one already applied.
- **Speed is keyed by encoder *and resolution*.** SD runs four times faster
  than 1080p on this box; pooling them is exactly how the first benchmark
  projection came out 1.75x optimistic.
- **A keeper with no software rung falls back to hardware and says so.**
  Encoding a film well but not perfectly beats leaving it out of the queue
  because the benchmark was never run with `--include-software`.
- **Keepers are night-only, enforced in the query that picks the next job.**
  That is why `decision.encoder` is a column rather than a dig into
  `detail_json`: the SQL has to be able to ask. `--now` overrides it, because
  that flag already means "ignore the schedule".
- **A missing keepers file is reported, not ignored.** "No keepers" and "the
  keepers list did not load" are different nights of encoding.

### What Phase 4 has not met

The guard has never talked to the real Sonarr or Radarr. Its tests drive a fake
one over httpx's `MockTransport`, so the request shapes are right by
construction, but two things can only be settled against the live instances:
whether `/customformat/schema` offers `ReleaseTitleSpecification` on those
versions (the guard stops rather than guessing if it does not), and what the
real match rate is. Both are printed by the dry run.

Calibration has nothing to calibrate against until `app work` has really run.

---

## Where we are

**Phase 0 is complete: the calibration ran on 2026-08-28 and hevc_vaapi won
outright. Direct play on the TVs is now the only unverified assumption left
before bulk encoding.**

The census settled the strategic question: **encoding is genuinely worth it
here.** The library is 77% H.264 by file count and 91% by bytes, with roughly
**8-12 TB reclaimable** against 3.9 TB free. The earlier worry that the library
might already be x265 was wrong -- that first sampled file was unrepresentative.

The constraint is now CPU time, not opportunity. At the measured 1080p speed
(~31 min per 45-minute episode) ~15,400 candidate files is **years** of
overnight encoding to do exhaustively, so queue order decides everything.
`policy.min_source_bytes` and the savings floor cut that a lot -- run
`app plan` for the real number rather than quoting this one.

- Repo: https://github.com/Belgregor71/vidsmasharr (public)
- `main`, local and remote in sync
- **Phases 0-4 are all built.** What remains is running them against the real
  box -- see the top of this file
- A movie calibration was still running when session 3 ended; its result is
  still unknown
- 292 tests passing (Phase 1 added 72, Phase 2 added 45, Phase 3 added 60,
  the ladder fix added 7, Phase 4 added 60)
- **Nothing has touched the media library yet.** Phase 3 now *can* -- it is
  the first code that deletes -- but it ships with `dry_run` on and
  `delete_original_on_success` off, and has never been run for real.

## Container status: VERIFIED WORKING (2026-08-28)

The capability report came back clean on the real DS1019+:

```
ffmpeg (encode): 7.1.4-Jellyfin
ffmpeg (score) : N-126277-ga8c7afa7d7-20260826
libvmaf filter : yes
hevc_vaapi WORKS | hevc_qsv WORKS | h264_vaapi WORKS | libx265 WORKS | libx264 WORKS
preferred hardware encoder: hevc_vaapi
```

No warnings. The two-binary split works. **The next thing to do is the
calibration run itself** — see Next Steps.

<details><summary>Rebuild command, if the container ever needs recreating</summary>

**Back up `config/` first.** Compose mounts `../config:/config`, so
`/volume1/docker/vidsmasharr/config` holds `vidsmasharr.db` (the library index
and every benchmark measurement), `profiles.yaml` and `config.yaml`. The
`rm -rf` below would take all of it. One line, as the terminal needs:

```sh
cd /volume1/docker && sudo cp -a vidsmasharr/config /volume1/scratch/vidsmasharr-config.bak && sudo rm -rf vidsmasharr vidsmasharr-main && sudo curl -sL https://github.com/Belgregor71/vidsmasharr/archive/refs/heads/main.tar.gz | sudo tar xz && sudo mv vidsmasharr-main vidsmasharr && sudo cp -a /volume1/scratch/vidsmasharr-config.bak/. vidsmasharr/config/ && cd vidsmasharr && sudo docker compose -f docker/docker-compose.yml build && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.capability
```

</details>

---

## Library census (2026-08-28, 300-file random sample)

Run it again any time with:
`bench.census --libraries /media/tv /media/movies /media/Anime`

| Codec | Files | Bytes |
|---|---|---|
| **h264** | **77.3%** | **91.0%** |
| hevc | 11.0% | 5.3% |
| mpeg4 | 10.3% | 2.9% |
| av1 | 1.0% | 0.8% |
| msmpeg4v3 | 0.3% | 0.1% |

Resolution: 1080p 40.3%, SD 35.7%, 720p 24.0%.
Dynamic range: SDR 90.7%, 10-bit 9.3%.

**Encode candidates: 66% of files, 86% of bytes.**
Rejected: 62 already-low-bitrate, 28 protected 10-bit, 11 already-efficient codec.

Extrapolated to the whole library:
- ~15,436 candidate files (**note: far more than the 6,000 originally assumed**)
- ~20.1 TB of candidate material
- 8.1 TB / 10.1 TB / 12.1 TB reclaimed at 40% / 50% / 60% reduction

### How to read it

- **Encoding is worth building.** Phases 2-3 are justified; this is not a
  dedupe-only project.
- **The 9.3% "unknown-10bit" is almost certainly the Anime library, not HDR.**
  10-bit x264/x265 is standard for anime because it suppresses banding in flat
  gradients. Protecting it is still correct -- 8-bit hardware output would
  reintroduce exactly the banding those encodes exist to avoid -- but it should
  not be mistaken for HDR movies.
- **SD is 35.7% of files but a small share of bytes.** Encoding SD has poor
  GB-saved-per-CPU-hour. The planner must not treat all files as equal.
- **Size skew is the whole strategy.** On a realistic distribution the largest
  20% of candidates hold roughly 60-65% of the available reclaim. Ordering the
  queue by predicted saving is worth more than any encoder tuning. The census
  now prints this curve directly.

### Now measured (2026-08-28)

~~The one real fps figure came from a 720p HEVC source policy would skip.~~
Answered by run `9ece8030435f`: **hevc_vaapi does 1080p at 33.7-35.9 fps**,
which is about **31 minutes per 45-minute episode**. See the calibration
section below -- and note that run's own "17.7 min/file" headline was wrong,
because it averaged SD speeds into the figure.

---

## Verified on the actual DS1019+

Do not re-derive these; they came from the box.

| Fact | Value |
|---|---|
| CPU | Intel Celeron J3455, 4 threads |
| Render node | `/dev/dri/renderD128`, **gid 937**, read/write from container |
| `hevc_vaapi` | **WORKS** (verified by real encode) |
| `hevc_qsv` | **WORKS** |
| `h264_vaapi`, `libx265`, `libx264` | WORK |
| vainfo HEVC encode | yes |
| Encode ffmpeg | jellyfin-ffmpeg 7.1.4 — no libvmaf (by design) |
| Score ffmpeg | static N-126277 at `/opt/ffmpeg-vmaf/bin/ffmpeg` — **libvmaf confirmed** |
| Media share | `/volume1/data/media/{tv,movies,Anime,music,youtube}` |
| Decoy share | `/volume1/Media` is 198MB, an empty leftover -- **not** the Plex library and not a duplicate |
| Live Plex DB | `/volume1/PlexMediaServer/AppData/Plex Media Server/Plug-in Support/Databases/` |
| Volume | 28TB total, **25TB used, 3.9TB free, 87%** |
| Scratch | `/volume1/scratch/vidsmasharr` (created) |
| Repo on NAS | `/volume1/docker/vidsmasharr` |
| Companion services (2026-08-28) | sonarr:8989, radarr:7878, prowlarr:9696, bazarr:6767, lidarr:8686, **tautulli:8181**, seerr:5055, maintainerr:6246 — all running |
| Plex itself | a DSM package, not a container (consistent with the `PlexMediaServer` path above) |

**The hardware plan is confirmed viable.** `hevc_vaapi` works, which was the
single biggest open risk in the whole project.

### Traps already hit and fixed — don't rediscover these

1. **DSM has no CFS bandwidth control.** `cpus:` in compose fails with
   "NanoCPUs can not be set". Use `cpu_shares` (now 512).
2. **Two similar-looking media shares.** The live library is
   `/volume1/data/media` (lowercase `tv`, `movies`). `/volume1/Media` also
   exists with `Movies`/`Television` inside and is a decoy — not the Plex
   library. Linux paths are case-sensitive, so neither forgives a near miss.
3. **Compose resolves relative volumes against the compose file's directory**,
   not the working directory. Hence `../config:/config`.
4. **jellyfin-ffmpeg omits libvmaf.** It is built for streaming. Encoding needs
   its VAAPI; scoring needs libvmaf; no single available build has both. The
   image now carries two binaries — see `VIDSMASHARR_FFMPEG_VMAF`.
5. **The Plex DB under `/volume1/@appstore/...` is the empty package template**,
   not the live library. Use the `PlexMediaServer` path above.
6. **No `git` on the NAS.** Updates are curl+tarball, which destroys any
   untracked `config.yaml`. **Installing Git from Package Center is the single
   highest-value chore outstanding** — see Open Questions.
7. **`--remove-orphans` would delete their `dockersocket` container**, which
   belongs to another stack. The orphan warning is cosmetic; ignore it.
9. **The rebuild command deletes the database.** `../config:/config` means
   the SQLite file, `profiles.yaml` and `config.yaml` all live inside the repo
   directory that `rm -rf vidsmasharr` removes. Losing it costs the whole
   calibration run and a full re-scan. Back `config/` up first -- see the
   rebuild command above -- or install Git and stop deleting the tree at all.
8. **`sudo cmd > /volume1/scratch/...` fails with "Permission denied."** The
   shell opens the redirect as the *login user* before `sudo` ever runs, and
   scratch is root-owned. Put the redirect inside root:
   `sudo sh -c 'nohup docker compose ... > /volume1/scratch/vidsmasharr/bench.log 2>&1 &'`
   The job number prints and then the job dies, so it looks like it started.

---

## Locked decisions (from a long Q&A — do not reopen)

| Area | Decision |
|---|---|
| TVs | One 4K + one 1080p, both smart TVs 2018+, HEVC direct play assumed **but not yet verified** |
| Player | Plex. One stored file must direct-play on both TVs; never per-TV copies |
| Encoder | Hybrid: hardware HEVC default, software x265 only for a hand-picked keepers list |
| 4K policy | Downscale **SDR** 4K → 1080p. **HDR/DV 4K is never touched** |
| Audio | Keep best English track (lossless → EAC3 640k 5.1), drop other languages, keep Eng + forced subs |
| Duplicates | **Report only.** Never auto-delete or auto-move. User decides in the UI |
| Replacement | Verify → atomic swap → delete original immediately (no hold folder) |
| Verification | ffprobe sanity checks on every file + sampled VMAF on 3 segments |
| Schedule | Overnight full speed; daytime throttled, auto-pause while Plex streams |
| Identity | Plex DB (primary) + Sonarr/Radarr APIs |
| *arr guard | Auto-write custom formats so HEVC is never an upgrade candidate — **with a dry-run diff shown first**. Built in session 4; note the score is **positive**, see below |
| Quality bar | Movies VMAF ~95, TV episodes ~92 |
| Stack | Python 3.11 + FastAPI + SQLite, Docker via Container Manager |
| UI | Web UI, Unmanic-style, server-rendered Jinja2 + HTMX |

**Implementation note (2026-08-28):** Tautulli is already running on :8181.
Its API is an easier source of "is anyone streaming right now?" than polling
Plex's own sessions endpoint, so the daytime auto-pause should look there first.

### The safety invariant that matters most

Apollo Lake can only encode HEVC in **8-bit**. Pushing HDR through it silently
produces SDR and destroys the grade permanently — with the original already
deleted. `app/scan/probe.py` therefore treats anything not *provably* 8-bit SDR
as protected: PQ/HLG transfer, Dolby Vision side data, BT.2020 primaries, bit
depth > 8, or an undeterminable stream.

`_bit_depth()` takes the **maximum** of `bits_per_raw_sample` and the `pix_fmt`
depth, because remuxes frequently disagree and the safe side of that
disagreement is "protected". A test covers exactly this case. **Do not
"optimise" that to trust one source.**

---

## What is built

```
app/config.py          YAML + env config. Safe defaults: dry_run=true,
                       delete_original_on_success=false
app/db.py              SQLite schema, 6 migrations, WAL
app/cli.py             app scan|identify|duplicates|phase1|plan|work|status|serve
app/scan/probe.py      ffprobe -> normalised facts + HDR detection  [CRITICAL]
app/scan/walker.py     library traversal, skip-dirs, reservoir sampling
app/scan/index.py      incremental sync + probe queue, unmounted-share guard
app/identity/plex.py   Plex library DB, snapshotted before reading
app/identity/arr.py    Sonarr/Radarr read-only clients
app/identity/filename.py  fallback parsing, never full confidence
app/identity/resolve.py   merges sources; prevents title splitting
app/dedupe/groups.py   duplicate grouping + keeper ranking (report only)
app/plan/rules.py      what to do with one file, and why       [CRITICAL]
app/plan/estimate.py   predicted output size and encode time
app/plan/profiles.py   profiles.yaml as production reads it
app/plan/planner.py    rank everything into a queue, write decisions
app/plan/calibrate.py  correct the estimator against jobs that really ran
app/plan/keepers.py    the hand-written list that gets software x265
app/guard/arr_guard.py stop the *arrs undoing the encoding  [writes OUTSIDE]
app/web/               FastAPI + Jinja2 UI on :8330
app/work/ffmpeg_cmd.py encode + VMAF command construction
app/work/streams.py    which audio and subtitle tracks survive
app/work/vmaf.py       run libvmaf, read the score back
app/work/verify.py     is this output safe to replace the original with?
app/work/swap.py       atomic install + delete      [the only deleting code]
app/work/schedule.py   night/day windows, pause while anyone is streaming
app/work/worker.py     the loop, and every safety gate in it
bench/capability.py    verifies encoders by real 1-second encodes
bench/runner.py        clip extraction, encode matrix, VMAF, decode-mode probe
bench/ladder.py        interpolates settings hitting VMAF targets -> profiles.yaml
bench/__main__.py      the 5-step Phase 0 run
docker/                two-ffmpeg image + DSM compose
tests/                 292 tests
```

**Phases 1 to 4 are built** (2026-08-28) and run end to end on synthetic data,
but none has been **run against the real library** -- that needs the NAS. See
`README.md` for the phase table.

### Phase 1 decisions worth not re-litigating

- **A library root that walks up empty is a mount failure, not a deletion.**
  If a root loses >50% of its known files the scanner refuses to mark anything
  missing and says so. Given the `/volume1/Media` decoy, this guard is not
  theoretical.
- **Files are never deleted from the database**, only flagged `missing`, so
  outcomes and history keep resolving.
- **Title splitting was the real risk in identity.** If Plex names a show and
  the filename parser names the same show for a file Plex has not scanned,
  two titles appear and the duplicate between them -- the one most worth
  finding -- is never found. A weak source therefore attaches to an existing
  title when the name matches *unambiguously*, and invents a new title when it
  cannot. Two "The Office" rows with no year to separate them is exactly the
  case where guessing wrong tells a user to delete the wrong file.
- **The keeper is the best *source*, not the smallest file.** A 4K HDR copy
  wins its group even though it is the biggest, because Phase 3 can shrink a
  keeper later and nothing can un-shrink a copy already thrown away.
- **Ambiguity is flagged, not resolved.** Differing runtimes (probably
  different cuts), HDR-vs-resolution trade-offs, and copies too close to rank
  are all marked *needs a human*.

---

## Session 3 (2026-08-28): Phase 2 built

Built while the calibration run was going on the NAS, so none of it has met the
real library yet. 161 tests pass (Phase 2 added 45).

```
app/plan/rules.py      what to do with one file, and why   [SAFETY-CRITICAL]
app/plan/estimate.py   predicted output size and encode time
app/plan/profiles.py   profiles.yaml as production reads it
app/plan/planner.py    rank everything into a queue, write decision rows
app/cli.py             + `app plan [--top N] [--provisional]`
app/web/               + the /plan page
```

`app plan` writes to the `decision` table and nothing else. Like the duplicate
report, it is a document.

### Phase 2 decisions worth not re-litigating

- **Protected content is never rewritten *at all*, remux included.** A stream
  copy looks safe because the video is untouched, but a Dolby Vision RPU can be
  lost in one. One invariant with no carve-outs is easier to keep true than one
  with an exception, and the exception would have bought only audio savings.
- **A file in an open duplicate group is never queued.** Encoding a copy the
  user is about to delete is the most expensive possible mistake on a box where
  the queue is measured in months. Settle the duplicate report first; the files
  become plannable the moment the group is resolved or dismissed.
- **Priority is GB saved per encode-hour.** This gets the census's "size-first"
  finding for free: encode time scales with duration and resolution, not with
  bitrate, so of two same-length 1080p episodes the fatter one earns four times
  as much from the same hour. Remuxes cost minutes rather than hours and float
  to the top on the same formula, with no special case.
- **`policy.min_source_bytes` defaults to 700MB**, which is the census's "SD
  may not be worth encoding at all" as a config knob. Set it to 0 to consider
  everything. The skip is always reported, never silent.
- **A plan made without a calibrated ladder is written in a `provisional`
  state, not `pending`.** Phase 3 selects on `pending`, so a guessed plan
  cannot be executed by accident. `app plan` refuses to run at all without
  profiles.yaml unless you pass `--provisional`.
- **The estimator runs two models and keeps the one predicting less saving.**
  A measured size ratio tracks how fat the source happened to be; a target
  bitrate does not. Constant-quality output depends on picture complexity
  rather than source bitrate, so the bitrate model is the more correct one --
  but where they disagree, under-promising is the safe direction for a queue
  this long. Every decision records which model produced its number, so Phase 4
  can check the estimates against the `outcome` table.
- **`EFFICIENT_CODECS` and the bitrate floors now live in `app/plan/rules.py`,
  and `bench/runner.py` imports them.** They were duplicated. The benchmark has
  to reject exactly what production rejects or the ladder gets calibrated on
  jobs that will never run, and two copies of that would have drifted silently.

### One thing the 2026-08-28 calibration did not produce

`bench/ladder.py` now records `expected_out_bitrate` per rung -- the measured
output bitrate, which is what the planner would rather estimate with. **The 2026-08-28 run was built from the tarball before that change**,
so its profiles.yaml will not carry the field. Nothing breaks: `load_ladder`
treats it as optional and the planner falls back to the policy target plus the
measured size ratio. To get the better estimates, rebuild the container and
re-run the benchmark at some later point -- it is not worth interrupting a
run that is already hours in.

---

## Session 3, part 2 (2026-08-28): Phase 3 built

Also built while the calibration ran. 221 tests pass (Phase 3 added 60).
**This is the first code in the project that can delete a file**, so both of
its safety switches ship off and every gate fails closed.

```
app/work/streams.py    which audio and subtitle tracks survive
app/work/vmaf.py       run libvmaf and read the score back
app/work/verify.py     is this output safe to replace the original with?
app/work/swap.py       atomic install and delete   [the only deleting code]
app/work/schedule.py   night/day windows, pause while anyone is streaming
app/work/worker.py     the loop, and every safety gate in it
app/cli.py             + `app work [--execute] [--limit N] [--now]
                              [--retry-failed] [--install-held]`
app/web/               + the /activity page
```

### Phase 3 decisions worth not re-litigating

- **Protection is re-checked at encode time against a fresh probe**, never
  trusted from the decision row, and the file's size and mtime are compared
  with the database first. A plan can be days old; an *arr upgrade that swapped
  an SDR release for an HDR one in between must not be encoded against stale
  facts. There is a test for exactly this: database says SDR, disk says HDR,
  disk wins.
- **Every VMAF sample must clear the bar, not their average.** Averaging would
  let one badly handled scene hide behind two easy ones, and that scene is the
  entire reason to check.
- **A verification that could not run is a failure.** No libvmaf, no readable
  output, no measurable duration -- all of them refuse the delete. The cost of
  a false "no" is one wasted encode; the cost of a false "yes" is a file that
  is gone.
- **A remux skips VMAF entirely.** The video stream is copied bit for bit, so
  there is nothing to score, and scoring it would add hours to the cheapest
  wins in the queue.
- **`--execute` overrides `dry_run` but never `delete_original_on_success`.**
  Starting work is a command you can type; authorising deletion stays a
  deliberate edit to config.yaml. This matters more than usual here because the
  NAS has no git and editing config is awkward -- see trap 6.
- **With deletion off, verified outputs are `held`, not `done`.** They sit in
  `/scratch/encoding` for the trial batch. Turning deletion on later and
  running `app work --install-held` installs them rather than spending those
  hours again. Marking them `done` would have quietly thrown that work away.
- **Being unable to ask whether anyone is streaming counts as "yes".** One
  video engine on the box; a stuttering film is the fastest route to this
  project being uninstalled, and the queue is months long either way, so an
  hour of caution costs nothing.
- **Failed decisions stick; stale plans do not.** A real failure (encode died,
  verification refused) stays `failed` and survives re-planning, so a bad file
  cannot climb back up a months-long queue -- `--retry-failed` clears them
  deliberately. A stale plan (file moved, changed, turned out protected) goes
  back to `skipped`, which the planner regenerates with fresh facts.
- **The install never leaves a moment with no file.** Copy to a temp file
  beside the target, fsync, check the size landed, `os.replace` (atomic within
  a directory), then delete the original only if the extension changed. A
  `.avi` becoming a `.mkv` has a window where both exist, which is the correct
  direction for that window to point.
- **The web header banner now reads from the config** instead of promising
  "nothing is deleted". A stale reassurance in the header would be worse than
  no banner.

### Still not verified on real hardware

Everything above passes on synthetic data with ffmpeg mocked out. **No real
encode has been run through this pipeline yet.** The first one should be a
`--limit 1` dry run, then a `--limit 1 --execute` with deletion still off, and
then a look at what lands in `/scratch/encoding`.

---

## Calibration run 9ece8030435f (2026-08-28) — DONE, but re-derive the ladder

60/60 encodes completed, no failures, VMAF on throughout. Hardware decode
works for every encoder. **`hevc_vaapi` is both faster and better than
`hevc_qsv` at every resolution**, which settles the encoder question:

| | vaapi | qsv |
|---|---|---|
| 1080p movie (VMAF 95) | qp 19, 33.7 fps | gq 20, 25.6 fps, **never reached 95** |
| 1080p tv (VMAF 92) | qp 26, 35.9 fps | gq 22, 27.9 fps |

`hevc_qsv` failed to reach VMAF 95 at 1080p at any tested setting (best 94.4).
Irrelevant in practice — vaapi is preferred and hits it — but it means qsv is
not a fallback for movies.

### The ladder from that run is optimistic, and was rebuilt

The first ladder said **1080p TV: qp 26, 14% size ratio**. The raw log for the
one clip we can read says qp 26 gave **39% size and VMAF 90.5** — below the 92
target. Both numbers came from the same group, so they cannot both be right.

Cause, since found and fixed: each group holds one measurement series *per
calibration clip*, all sharing the same quality values. Those duplicate x
values went straight into the interpolator, which followed whichever clip
sorted first — in practice the easiest one. So the setting was chosen by the
clip that compressed best and the size ratio was quoted from it too.

`bench/ladder.py` now aggregates per setting before interpolating: **VMAF by
`min`** so the hardest sampled content governs the setting, size and speed by
`mean` because those are expectations rather than promises. It also warns when
clips disagree by more than the verification margin, which they did here.

Expect the corrected 1080p TV rung to land nearer **qp 23-24 at ~30-35%**
rather than qp 26 at 14%. Still a very good result; just an honest one.

**Re-derive it without re-encoding.** Every measurement is in `bench_result`,
so this costs seconds rather than a night:

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.ladder
```

### The timeline projection was wrong too, in the other direction

It reported **61 fps / 17.7 min per file**. That averaged every hardware rung
together including SD, which runs at 97-157 fps and holds almost none of the
bytes. No 1080p file will ever see 61 fps. Projection now uses the 1080p rungs
only: **roughly 31 min per 45-minute episode**, about 1.75x the reported
figure. The `6000 files` in that output was always a placeholder — the census
says ~15,400 candidates — and the real answer now comes from `app plan`, which
counts actual files instead of assuming an average.

### What the ratios say about policy

- **SD is confirmed not worth encoding.** Movie SD came back at 77-82% of
  source, well inside the 20% savings floor, so the planner skips it anyway.
  `policy.min_source_bytes` (700MB) removes most of it before that.
- **720p movie at 33% and 1080p at 39%** — the reclaim estimate holds.
- Phase 0 measured that hardware decode works for every encoder. That answer
  now travels: the planner records it on each decision and the worker builds
  the command with it, instead of assuming.

### The sweep floor is too high for the movie target

The rebuilt ladder exposed the next problem. **Five of the six groups report
that no tested setting reached VMAF 95** -- the hardest clip topped out at 94.9
even at qp 20, the finest value swept. Every movie rung is therefore pinned to
the sweep floor, and its size ratio is whatever that setting happened to give
rather than a calibrated one. It shows: movie 720p came back at 68% of source
and movie SD at 87%, which is not worth encoding at all.

The TV rungs (target 92) are properly bracketed and trustworthy. TV is the bulk
of the library, so this does not block progress -- it blocks *movies*.

`QP_SWEEP` is now `--qp-sweep`, so fixing it does not need a code edit. A
targeted movie run is far cheaper than the original night: two sources, three
quality points, movies only.

**Combine runs rather than replacing them.** `bench` writes profiles.yaml from
the run it just did, so a movies-only run on its own would drop every TV rung
measured on 2026-08-28. `bench.ladder --run-id A B` builds one ladder from
several runs, which is the point of storing every measurement.

### Still outstanding, and the run says so itself

**Direct play has not been verified on either TV.** Encode 2-3 real files at
the corrected ladder settings, put them in Plex, and confirm both TVs say
*Direct Play* rather than *Transcode*. If either transcodes, the CPU cost moves
to playback and the whole HEVC plan needs revisiting — stop there rather than
encoding thousands of files.

---

## Next steps, in order

1. ~~Rebuild and confirm libvmaf.~~ **Done 2026-08-28.**
2. ~~Library census.~~ **Done 2026-08-28** — encoding is worth it, see above.
3. ~~**Run the real calibration.**~~ **Done 2026-08-28**, run `9ece8030435f`. Re-derive the ladder after rebuilding, see above.
   <details><summary>the original instructions</summary>

   **Run the real calibration** on representative H.264 content. The candidate
   filter now rejects already-efficient sources automatically, so this should
   land on real material:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr      bench --libraries /media/tv /media/movies --sources 4 --keep-clips
   ```
   Background it — the full matrix is hours, and an SSH drop kills it:
   ```sh
   sudo nohup docker compose -f docker/docker-compose.yml run --rm -T vidsmasharr      bench --libraries /media/tv /media/movies --sources 4 --keep-clips      > /volume1/scratch/vidsmasharr/bench.log 2>&1 &
   ```
   What matters in the output: **fps on 1080p H.264** (replaces the invalid
   35.5 figure) and **size ratio at the chosen QP** (validates the 40-60%
   reduction assumption the whole 8-12 TB estimate rests on).

   </details>
4. **Verify direct play** — encode 2-3 real files at the ladder settings, play
   one on each TV, confirm Plex says "Direct Play" not "Transcode". Do not skip
   this; if either TV transcodes the codec choice must change.
5. **Run the plan for real**, once the calibration has written
   `config/profiles.yaml`:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app plan
   ```
   It refuses to run without a calibrated ladder; `--provisional` shows the
   shape of the plan anyway, in a state Phase 3 cannot execute. Run
   `app duplicates` first -- files in an unresolved duplicate group are
   deliberately held out of the queue.
6. **Run one real encode.** Phase 3 is built but has never met real media.
   Work up to it in three steps, checking the output at each:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app work --limit 1
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app work --limit 1 --execute
   ```
   The first prints the ffmpeg command it would run. The second really encodes,
   verifies, and leaves the output in `/scratch/encoding` -- `delete_original_on_success`
   is off, so the library is untouched. Watch that file on both TVs before
   going any further. Then turn deletion on in `config.yaml` and use
   `app work --install-held` so the encode is not repeated.
7. ~~**Phase 4**: the *arr guard, estimator calibration, x265 keepers.~~
   **Built 2026-08-28** — see the top of this file for how to run each, and in
   what order relative to steps 4-6. The guard goes in *before* bulk encoding;
   calibration only works *after* a real batch.

Steps 4 and 5 both need Phase 1 to have run on the real library first:

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app phase1
```

~~**Phase 1** — scan, identify, duplicate report + UI.~~ **Built 2026-08-28**,
not yet run for real. Expect the first scan to take a while -- it ffprobes every
file once. It is largest-first and resumable, and `--probe-limit N` caps a run.
Needs the Plex token and *arr API keys in `config/config.yaml` to resolve
identity from anything better than filenames.

### A design consequence the census forces

The planner's priority formula was `est_bytes_saved / est_cpu_seconds`. The
census shows that is even more important than assumed, and suggests two
refinements worth building into Phase 2:

- **Size-first ordering delivers most of the win early.** Encoding the largest
  20% of candidates reclaims ~60-65% of the available space. The queue should
  be explicitly biased that way rather than relying on the ratio alone.
- **SD content may not be worth encoding at all.** 35.7% of files, a small
  share of bytes, and the same per-file overhead. Consider a configurable
  minimum source size below which files are skipped regardless of ratio.

## Open questions for the user

- **Install Git on the NAS?** Recommended. Four more phases of iteration ahead,
  and once `config.yaml` holds real credentials, the curl+tarball update path
  destroys it every time.
- **Plex token, Sonarr URL/key, Radarr URL/key** — needed for Phase 1 identity
  resolution. Not yet gathered. They go in `config/config.yaml`, which is
  **gitignored** — never in `config.example.yaml`.
- ~~**Is Tdarr still active?**~~ **Answered 2026-08-28: no Tdarr container is
  running.** `docker ps` shows 26 containers and none is Tdarr, so the
  `@tdarr-ffmpeg` core dump is historical. Nothing else is competing for
  `/dev/dri`. Still worth a glance at Package Center in case a stopped
  container has a schedule attached.
- ~~**Sonarr confirmed?**~~ **Answered 2026-08-28: yes**, `sonarr` is up on
  :8989 (`lscr.io/linuxserver/sonarr`). Radarr :7878, Prowlarr :9696,
  Bazarr :6767, Lidarr :8686 also confirmed running.
- **Library file counts** unknown. Needed to replace the guessed `6000` in the
  timeline projection -- though `app plan` now computes the real figure from
  actual files, so this only matters for the benchmark's rough projection.
- **Is VMAF 95 reachable for movies on this hardware?** The first sweep said
  no (best 94.9 at the finest setting tried). If the finer sweep also says no,
  that is a decision for the user: lower `quality.movie_vmaf`, or accept that
  movies get the finest setting available and a modest size reduction.
- **Does the *arr guard's custom format actually match our re-encoded files?**
  It matches on the release title, and our encode keeps the original name,
  which usually says `x264`. `app arr-guard` samples the live instances and
  prints the real match rate. If it is low, the only fix is putting
  `{MediaInfo VideoCodec}` in the *arr naming format and renaming the library
  — which rewrites filenames Plex has already indexed, so it is a deliberate
  choice, not something the guard will do on its own.
- **Should `--neutralise` be used?** Only if the dry run reports an existing
  negative HEVC score. It raises those to zero, and they are the user's own
  tuning, so it is their call.

## Note for the assistant

The user's terminal mangles multi-line pastes — **give shell commands as a
single line**, chained with `&&`. Their GitHub commits use
`Belgregor71@users.noreply.github.com`; the repo is public, so never commit the
personal address.
