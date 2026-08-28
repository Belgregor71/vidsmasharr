# Handover — sessions 1–2 (2026-08-27 → 28)

Read this first. It records what is *verified* on the real hardware versus what
is still assumed, so tomorrow doesn't re-litigate settled decisions or trust
unverified ones.

---

## Where we are

**Phase 0 is unblocked and the library census is done. The calibration run is
still outstanding.**

The census settled the strategic question: **encoding is genuinely worth it
here.** The library is 77% H.264 by file count and 91% by bytes, with roughly
**8-12 TB reclaimable** against 3.9 TB free. The earlier worry that the library
might already be x265 was wrong -- that first sampled file was unrepresentative.

The constraint is now CPU time, not opportunity. ~15,400 candidate files is
roughly **two years** of overnight encoding to do exhaustively, so queue order
decides everything.

- Repo: https://github.com/Belgregor71/vidsmasharr (public)
- 5 commits, `main`, local and remote in sync
- 116 tests passing (Phase 1 added 72)
- Nothing has touched the media library. Phase 0 only reads and writes scratch.

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

```sh
cd /volume1/docker && sudo rm -rf vidsmasharr vidsmasharr-main \
  && sudo curl -sL https://github.com/Belgregor71/vidsmasharr/archive/refs/heads/main.tar.gz | sudo tar xz \
  && sudo mv vidsmasharr-main vidsmasharr && cd vidsmasharr \
  && sudo docker compose -f docker/docker-compose.yml build \
  && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.capability
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

### Not yet measured

The one real fps figure so far (35.5 fps, 30.4 min/file) came from a **720p
HEVC** source that policy would skip, so it is not valid. Encode speed on
representative 1080p H.264 is still unknown, and every timeline estimate
depends on it.

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
| *arr guard | Auto-write custom formats so HEVC is never an upgrade candidate — **with a dry-run diff shown first** |
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
app/db.py              SQLite schema, 5 migrations, WAL
app/cli.py             app scan|identify|duplicates|phase1|status|serve
app/scan/probe.py      ffprobe -> normalised facts + HDR detection  [CRITICAL]
app/scan/walker.py     library traversal, skip-dirs, reservoir sampling
app/scan/index.py      incremental sync + probe queue, unmounted-share guard
app/identity/plex.py   Plex library DB, snapshotted before reading
app/identity/arr.py    Sonarr/Radarr read-only clients
app/identity/filename.py  fallback parsing, never full confidence
app/identity/resolve.py   merges sources; prevents title splitting
app/dedupe/groups.py   duplicate grouping + keeper ranking (report only)
app/web/               FastAPI + Jinja2 UI on :8330
app/work/ffmpeg_cmd.py encode + VMAF command construction
bench/capability.py    verifies encoders by real 1-second encodes
bench/runner.py        clip extraction, encode matrix, VMAF, decode-mode probe
bench/ladder.py        interpolates settings hitting VMAF targets -> profiles.yaml
bench/__main__.py      the 5-step Phase 0 run
docker/                two-ffmpeg image + DSM compose
tests/                 116 tests
```

**Phase 1 is built** (2026-08-28) and runs end to end on synthetic data, but
has **not yet been run against the real library** -- that needs the NAS.
Phases 2-4 (planner, encode pipeline, *arr guard) are not started. See
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

## Next steps, in order

1. ~~Rebuild and confirm libvmaf.~~ **Done 2026-08-28.**
2. ~~Library census.~~ **Done 2026-08-28** — encoding is worth it, see above.
3. **Run the real calibration** on representative H.264 content. The candidate
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
4. **Verify direct play** — encode 2-3 real files at the ladder settings, play
   one on each TV, confirm Plex says "Direct Play" not "Transcode". Do not skip
   this; if either TV transcodes the codec choice must change.
5. ~~**Phase 1** — scan, identify, duplicate report + UI.~~ **Built
   2026-08-28.** Not yet run on the real library. First real run:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app phase1
   ```
   Expect the first scan to take a while -- it ffprobes every file once. It is
   largest-first and resumable, and `--probe-limit N` caps a run. Needs the
   Plex token and *arr API keys in `config/config.yaml` to resolve identity
   from anything better than filenames.

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
  timeline projection.

## Note for the assistant

The user's terminal mangles multi-line pastes — **give shell commands as a
single line**, chained with `&&`. Their GitHub commits use
`Belgregor71@users.noreply.github.com`; the repo is public, so never commit the
personal address.
