# Handover — end of session 1 (2026-08-27)

Read this first. It records what is *verified* on the real hardware versus what
is still assumed, so tomorrow doesn't re-litigate settled decisions or trust
unverified ones.

---

## Where we are

**Phase 0 (calibration) is written and unit-tested. It has not yet produced
real numbers from the NAS.** The blocker chain is nearly cleared — five fixes
deep — with one rebuild outstanding.

- Repo: https://github.com/Belgregor71/vidsmasharr (public)
- 5 commits, `main`, local and remote in sync
- 44 tests passing, ~2,700 lines
- Nothing has touched the media library. Phase 0 only reads and writes scratch.

## THE ONE COMMAND TO RUN FIRST

The VMAF fix (commit `5ced61c`) is pushed but **not yet built or verified on the
NAS**. Everything below depends on it working.

```sh
cd /volume1/docker && sudo rm -rf vidsmasharr vidsmasharr-main \
  && sudo curl -sL https://github.com/Belgregor71/vidsmasharr/archive/refs/heads/main.tar.gz | sudo tar xz \
  && sudo mv vidsmasharr-main vidsmasharr && cd vidsmasharr \
  && sudo docker compose -f docker/docker-compose.yml build \
  && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.capability
```

Success looks like `libvmaf filter : yes` and two different ffmpeg versions on
the `(encode)` and `(score)` lines. If the ~100MB static ffmpeg download failed,
the build still succeeds but prints a warning and libvmaf stays `NO`.

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
| Encode ffmpeg | jellyfin-ffmpeg 7.1.4 — **has no libvmaf** |
| Media share | `/volume1/Media/{Movies,Television,Music}` (capital M) |
| Live Plex DB | `/volume1/PlexMediaServer/AppData/Plex Media Server/Plug-in Support/Databases/` |
| Volume | 28TB total, **25TB used, 3.9TB free, 87%** |
| Scratch | `/volume1/scratch/vidsmasharr` (created) |
| Repo on NAS | `/volume1/docker/vidsmasharr` |

**The hardware plan is confirmed viable.** `hevc_vaapi` works, which was the
single biggest open risk in the whole project.

### Traps already hit and fixed — don't rediscover these

1. **DSM has no CFS bandwidth control.** `cpus:` in compose fails with
   "NanoCPUs can not be set". Use `cpu_shares` (now 512).
2. **Linux is case-sensitive.** The share is `/volume1/Media`, not `/media`.
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
app/scan/probe.py      ffprobe -> normalised facts + HDR detection  [CRITICAL]
app/scan/walker.py     library traversal, skip-dirs, reservoir sampling
app/work/ffmpeg_cmd.py encode + VMAF command construction
bench/capability.py    verifies encoders by real 1-second encodes
bench/runner.py        clip extraction, encode matrix, VMAF, decode-mode probe
bench/ladder.py        interpolates settings hitting VMAF targets -> profiles.yaml
bench/__main__.py      the 5-step Phase 0 run
docker/                two-ffmpeg image + DSM compose
tests/                 44 tests: HDR detection, ladder interpolation, commands
```

Phases 1–4 (scan/dedupe, planner, encode pipeline, *arr guard) are **not
started**. See `README.md` for the phase table.

---

## Next steps, in order

1. **Run the rebuild command above.** Confirm `libvmaf filter : yes`.
2. **Run the real benchmark** against the Television library:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr \
     bench --libraries /media/Television --sources 4
   ```
   Takes an evening. Produces `config/profiles.yaml` and a timeline projection.
   Expect the projection to be sobering — 25TB is well beyond a year of
   overnight encoding, which is *why* dedupe runs first.
3. **Verify direct play** — encode 2–3 real files with the ladder settings, play
   one on each TV, confirm Plex says "Direct Play" not "Transcode". **Do not
   skip this.** If a TV transcodes, the codec choice must change before any bulk
   work.
4. **Then start Phase 1** (scan + identify + duplicate report). This is where
   the user first gets real value: on a 25TB library, duplicates are very likely
   the largest single reclaim and cost almost no CPU.

---

## Open questions for the user

- **Install Git on the NAS?** Recommended. Four more phases of iteration ahead,
  and once `config.yaml` holds real credentials, the curl+tarball update path
  destroys it every time.
- **Plex token, Sonarr URL/key, Radarr URL/key** — needed for Phase 1 identity
  resolution. Not yet gathered. They go in `config/config.yaml`, which is
  **gitignored** — never in `config.example.yaml`.
- **Is Tdarr still active?** A `@tdarr-ffmpeg` core dump was seen on the NAS.
  If it still has scheduled work it must be disabled — two tools re-encoding the
  same library would fight over files and `/dev/dri`.
- **Sonarr confirmed?** Radarr, Prowlarr and qBittorrent were all evidenced by
  core dumps; Sonarr was not directly seen.
- **Library file counts** unknown. Needed to replace the guessed `6000` in the
  timeline projection.

## Note for the assistant

The user's terminal mangles multi-line pastes — **give shell commands as a
single line**, chained with `&&`. Their GitHub commits use
`Belgregor71@users.noreply.github.com`; the repo is public, so never commit the
personal address.
