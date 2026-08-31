# Handover — sessions 1–7 (2026-08-27 → 31)

Read this first. It records what is *verified* on the real hardware versus what
is still assumed, so tomorrow doesn't re-litigate settled decisions or trust
unverified ones.

---

## NEXT SESSION: start here

**All five phases are built and every code question is answered.** What remains
is running it against the real box, and the order matters: the cheap checks
that could invalidate everything come before the expensive work.

### State at a glance (2026-08-31)

| | where it stands |
|---|---|
| Code | Phases 0-4 built, 345 tests, **`main` at `485c74c`**. `ladder-robust` is merged and deleted -- everything it carried is on `main`, which is the only branch on the remote |
| NAS repo | **NOT a git checkout -- `git` is not on PATH.** But md5-swept 2026-08-31: tree *and* image are `485c74c` throughout, bar 4 inert test files. See below |
| TV ladder | **written to `profiles.yaml` 2026-08-29** -- hevc_vaapi qp 22 -> 39% at 1080p |
| Movie ladder | **absent, deliberately** -- no valid movie calibration exists yet |
| Wider TV benchmark | **finished 2026-08-29**, folded in, 160 measurements total |
| Direct play | **VERIFIED 2026-08-30 -- Direct Play on both TVs.** No longer a blocker |
| *arr guard | **APPLIED 2026-08-30** with `--neutralise`; second pass says "nothing to write" |
| NAS `config.yaml` | **complete 2026-08-30.** Plex token, Tautulli key, both *arr keys, correct `path_map`. No FILL MEs left |
| NAS access | **SSH + passwordless `docker` from the workstation**, see below |
| Phase 1 on the real library | **DONE 2026-08-30.** 23,287 files, 23,078 probed, 9 unprobeable, all resolved |
| Duplicates | **238 group(s), 684 files, 166 GB reclaimable.** 150 need a human decision |
| Plan | **RE-RUN 2026-08-30** after the estimator fix. 4,850 jobs, 5,437 GB, 2,868 encode-hours |
| First real jobs | **DONE 2026-08-30.** Two remuxes, verified, 3.76 GiB, `held` in scratch |
| Phase 3 deadlock | **FIXED `50c244b`.** Nothing ran before it; do not run an older build |
| Dropped fonts | **FIXED `2561f41`.** The first output lost 15 font attachments. See below |
| Estimator | **FIXED `e373bb0`** (AAC by channel). Look Back landed exact; Kiki still 30% over |
| Outcomes recorded | **2.** `app calibrate` needs 8 before it will produce a factor |
| Playback on TV | **PASSED 2026-08-31.** Both test files watched on both TVs, both good against the step 6 checks |
| Test copies | `/volume1/data/media/vidsmasharr-test/` -- **testing is done; delete this folder.** Not the scratch copies |
| Next action | **Turn on `delete_original_on_success`, then `app work --install-held`** -- installs the two outputs already in scratch rather than redoing the work |
| Media library | **untouched.** Nothing has been encoded, moved or deleted |

---

## Git on the NAS does not work -- read before typing any command here

A previous session recorded that Git Server was installed and the tree
converted in place, and that `sudo git pull` was the way to update. **Settled 2026-08-30: git was never installed.** There is no binary at
`/usr/local/bin/git`, `/opt/bin/git`, `/usr/bin/git`, and no
`/volume1/@appstore/Git` package directory. The earlier session wrote up the
`git init` conversion from intent, not from a successful run.

```
$ sudo git pull
sudo: git: command not found
$ sudo env "PATH=$PATH" git pull
env: 'git': No such file or directory
```

**A second, separate cause of "command not found" on this box**, found the
same day and worth knowing before it wastes an hour: the login user's `PATH`
is `/usr/bin:/bin:/usr/sbin:/sbin` -- **`/usr/local/bin` is not on it.** That
is where DSM symlinks package binaries, `docker` and `docker-compose`
included. So `docker` looks missing to an interactive shell and works fine
under `sudo`, which uses its own `secure_path`. `visudo` is missing from that
`secure_path` in turn. When something is "not found" here, look for it before
concluding it is absent:

```sh
ls -l /usr/local/bin/ | grep -i <name>
```

**Do not trust any instruction in this file that begins with `git`.** To
re-check the git situation:

```sh
ls -d /volume1/@appstore/Git/bin/git /opt/bin/git /usr/local/bin/git /usr/bin/git 2>/dev/null; ls -d /volume1/docker/vidsmasharr/.git 2>/dev/null
```

**How code actually got deployed on 2026-08-29**, and the pattern to reuse. The
repo is public, the image bakes the source in (`docker/Dockerfile` does
`COPY app /app/app` and `COPY bench /app/bench`, and compose mounts only
`config`, `scratch`, `media` and `plexdb`), so a changed file needs a rebuild
before it runs -- editing on disk alone changes nothing:

```sh
cd /volume1/docker/vidsmasharr && sudo cp bench/ladder.py /volume1/scratch/ladder.py.main.bak && sudo curl -fsSL -o bench/ladder.py https://raw.githubusercontent.com/Belgregor71/vidsmasharr/main/bench/ladder.py && sudo docker compose -f docker/docker-compose.yml build
```

The rebuild is quick: `pyproject.toml` is unchanged, so the apt, ffmpeg and pip
layers all come from cache and only the two `COPY` layers re-run. Confirm the
container is running what you think before reading any output from it --
nothing warns you if it is not:

```sh
sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.ladder --help | grep -c robust
```

The tree got there by hand-patching, one file at a time, from what was then
`ladder-robust` -- which has since been merged and deleted, so the command
above originally pulled from a branch whose URL 404s now. Two earlier drafts of
this file named two different sets of "four hand-patched files", seven distinct
paths between them, and neither was checkable without measuring.

**Measured 2026-08-31 by md5 sweep -- the drift is gone.** Every tracked file
under `app/`, `bench/`, `docker/` and `config/` was hashed on the NAS and
against the blobs at `485c74c`:

| | result |
|---|---|
| **59 of 64** tracked files | **byte-identical to `main` at `485c74c`** |
| 4 files | stale, all under `tests/` -- `test_arr_guard`, `test_ladder`, `test_plan`, `test_work`, still at `e842d30` |
| 1 file | `config/config.example.yaml`, an older revision than either commit |
| absent | none -- nothing tracked is missing from the box |

**The running image was hashed too, not just the tree**, because the image
bakes the source in and nothing warns you when they disagree: ten files
including `worker.py`, `streams.py`, `estimate.py` and `ladder.py` all match
`485c74c` inside the container. **The tree was rebuilt after the last patch.**

So the NAS is effectively a clean `485c74c` checkout for every purpose that
runs. **The four stale tests are inert** -- `docker/Dockerfile` copies only
`app` and `bench`, so `tests/` is never in the image and cannot be run there.
The stale `config.example.yaml` never executes either; it only misleads
whoever copies from it next, and it predates the `tautulli` block, the *arr
`timeout` and `notify_on_replace`, and the corrected `path_map` comment. The
live `config/config.yaml` is unaffected and correct.

Pre-patch backups still sit in `/volume1/scratch/`: `ladder.py.main.bak`,
`worker.py.main.bak`, `streams.py.main.bak`, `estimate.py.main.bak`. They are
superseded now and safe to delete.

**Two pieces of litter found by the same sweep**, both root-owned, neither
touching anything that runs:

- **`config/config/`, and `config/config/config/`, four levels deep** -- an
  earlier bind mount pointed at itself. Each level holds its own stale
  `profiles.yaml` and `vidsmasharr.db`, all dated 2026-08-28. The **127 KB**
  `vidsmasharr.db` in there is not the real one; the real one is **45 MB** at
  `config/vidsmasharr.db`. Point a command at the wrong one and it will
  silently look like a library of nothing.
- **`config/.config.yaml.swp`** -- a vim swap file from a killed editor session
  on 2026-08-30. Harmless until someone opens `config.yaml` in `vi` and gets a
  recovery prompt.

**How to write to that tree at all**, since it is root-owned and the sudo
grant covers only the docker binary -- `sudo cp` and `sudo curl` both fail
here, whatever the older notes say. Pipe the file through a container that
mounts the repo, which `cat >` truncates in place so ownership and mode
survive:

```sh
tr -d '\r' < app/work/worker.py \
  | ssh -i ~/.ssh/nas_synology BrettGreg@192.168.0.179 \
    'sudo /usr/local/bin/docker run --rm -i -v /volume1/docker/vidsmasharr:/repo \
     alpine:latest sh -c "cat > /repo/app/work/worker.py"'
```

Then rebuild, and check the hash the image actually carries -- md5 both ends,
because nothing warns you when the container is running last week's code.
Getting git working, or re-deploying every file from one branch, is worth doing
before the next code change.

---

## Driving the NAS from the workstation (set up 2026-08-30)

The assistant can now run commands on the NAS directly, which is how most of
2026-08-30 got done. Two pieces:

**SSH key auth already existed.** `~/.ssh/nas_synology` on the workstation is
authorised for `BrettGreg@192.168.0.179`. There is no `Host` entry in
`~/.ssh/config` for it, so the key must be named explicitly:

```sh
ssh -i ~/.ssh/nas_synology BrettGreg@192.168.0.179 '<command>'
```

**Passwordless sudo, scoped to the docker binary.** Everything this project
runs needs root, and a non-interactive SSH session cannot answer a password
prompt. The grant is one drop-in file:

```
BrettGreg ALL=(ALL) NOPASSWD: /usr/local/bin/docker
```

written as `/etc/sudoers.d/vidsmasharr-docker`, mode 440. **To revoke:**
`sudo rm /etc/sudoers.d/vidsmasharr-docker`. Always call the binary by its full
path -- `sudo -n /usr/local/bin/docker ...` -- since the rule matches the
resolved command.

**Be honest about what that grants.** Passwordless docker as root is
root-equivalent: anything that can start a container can mount `/` into one. It
is an *accident* boundary, not a security one. What it does buy is that a stray
command outside Docker still fails.

**Reading and editing `config.yaml` without a second sudo grant.** `config/` is
a bind mount, so the file is reachable through a container:

```sh
cd /volume1/docker/vidsmasharr && sudo -n /usr/local/bin/docker compose -f docker/docker-compose.yml run --rm --entrypoint cat vidsmasharr /config/config.yaml
```

For an edit, run a script instead of `cat` -- and note `PYTHONPATH=/app` plus
`cd /app` are needed when overriding the entrypoint, because the image's
`ENTRYPOINT ["python3", "-m"]` is what normally puts the app on the path.

**Backgrounding a long run.** `nohup` it, and redirect to the **home
directory**, not `/volume1/scratch` -- with sudo scoped to docker, the shell
opening the redirect is the login user and cannot write to scratch:

```sh
cd /volume1/docker/vidsmasharr && nohup sudo docker compose -f docker/docker-compose.yml run --rm -T vidsmasharr app phase1 > ~/phase1-full.log 2>&1 &
```

---

### 1. The wider TV benchmark finished -- write the ladder

**Done 2026-08-29.** 10 more TV clips (Fringe, Minx, Resident Alien, Secret
Level, Willow), 100 measurements, folded in with the 60 from 2026-08-28 for 160
total. It changed the answer, and it changed how the ladder is built.

**The 15% at 1080p is gone.** Pooled naively the rung came out at **qp 20 and
56% of source**, with "no tested setting reached VMAF 92" on every rung except
SD. The cause was not the content, it was `_aggregate(..., "min")`: nine of ten
clips reached 92 between qp 22 and 26, and the tenth (Willow S01E08, dark and
grainy) topped out at 90.8 and never reached it at all. Because the VMAF curves
were pooled by minimum *before* interpolating, that one clip dragged the pooled
curve below the target everywhere and the rung fell off the end of the sweep.

`bench.ladder --robust` (on `main` since the merge, off by default) interpolates
each clip separately first, names and sets aside the ones that cannot reach the
target, and takes the second-hardest of the survivors. The argument for it: a
file harder than the rung is already caught per file by verification failing
closed at VMAF 89, whereas tuning the whole library down to it is paid on every
file -- and CPU is the resource this project rations.

| encoder | res | strict min | `--robust` | clips used-aside |
|---|---|---|---|---|
| hevc_vaapi | 1080p | qp 20 / 56% | **qp 22 / 39%** / 33.0 fps | 9-1 |
| hevc_vaapi | 720p | qp 23 / 38% | **qp 24 / 33%** / 31.7 fps | 4-0 |
| hevc_vaapi | sd | qp 24 / 50% | qp 24 / 50% / 168.5 fps | 2-0 |

On the 1080p share of the library (~8.1 TB of candidates) that is ~4.4 TB
reclaimed after an assumed ~10% verification-reject tail, against ~3.6 TB for
strict min. Speed cost: 33.0 fps against 37.5, so a 45-minute episode goes from
~29 to ~33 minutes.

**Written 2026-08-29** with the command below. `hw_decode` was carried forward
from the previous `profiles.yaml` (`hevc_vaapi`, `hevc_qsv`, `h264_vaapi` all
true) rather than lost, which is what you want -- production must decode the
same way the benchmark measured or the fps figures are a fiction.

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr bench.ladder --all-runs --run-class 9ece8030435f=tv --robust
```

The run reported `labelled 60 row(s) as tv -- stored`, so **`--run-class` is no
longer needed**: rebuild with `--all-runs --robust` alone. Note that `--robust`
*is* still needed every time -- it is a flag, not a stored property of the
measurements, and rebuilding without it silently returns the qp 20 / 56% rung.

**hevc_vaapi wins by more than the sizes suggest.** At 1080p hevc_qsv could not
reach VMAF 92 on **3 of 10 clips**; vaapi failed on 1. qsv's rung prints a
smaller size ratio (36% vs 39%) only because it is averaged over the easier
seven clips that survived -- which is why the ladder now prints a clips column.
Do not read a size ratio across two rungs without checking it.

**Still open on the ladder:** `--robust` sets the rung at the second-hardest of
nine surviving clips, which is the 22nd percentile -- probably still
conservative. Rank 2 gives qp 23 / 30% and the median gives qp 24 / 25%. Do not
guess: run the first real batch at the current setting, then `app calibrate`
measures the actual reject rate and the evidence decides. If nothing fails
verification, this was too cautious.

### 2. Verify direct play on both TVs -- DONE, and it passed

**2026-08-30: both TVs report Direct Play.** A file encoded at the shipping
1080p rung (`hevc_vaapi`, `qp 22`, `eac3` 640k 5.1) plays untranscoded on both
sets, including the 1080p file on the 4K one. The last big assumption behind
the whole plan is confirmed: HEVC costs CPU once at encode time, not on every
playback.

That was the blocker -- if either TV had transcoded, the codec choice, the
ladder and the *arr guard would all have needed revisiting. Nothing downstream
is gated on it any more. Proceed to step 3.

The `hevc-test` folder and its Plex library were removed afterwards, so
nothing of the test is left in the media share.

<details><summary>how it was tested, if it ever needs redoing</summary>

**Read the verdict in Tautulli** (already running on :8181) -> Activity. It
shows `Direct Play` / `Direct Stream` / `Transcode` *and the reason*, which is
the part that matters.

**First, on each TV's Plex app: set Video Quality to Original/Maximum, and turn
subtitles off.** A client quality cap forces a transcode whatever the file is,
and burned-in subtitles force one on their own. This is the most common way the
test lies to you.

Make a test file at the real ladder settings -- the iGPU must be free, so not
while a benchmark is running. **The `-qp 22` below is the current 1080p rung**
(it was 26 before the 2026-08-29 rebuild; if you rebuild the ladder again,
re-read the rung before making this file). QP does not change whether a client
direct-plays -- codec, profile, level, bit depth and resolution decide that --
but the higher-bitrate file is the more conservative test, so test at the
setting you will actually ship:

```sh
sudo mkdir -p /volume1/data/media/hevc-test && cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm --entrypoint ffmpeg vidsmasharr -hide_banner -nostdin -y -init_hw_device vaapi=va:/dev/dri/renderD128 -filter_hw_device va -hwaccel vaapi -hwaccel_output_format vaapi -i "/media/tv/SHOW/Season 1/EPISODE.mkv" -map 0:v:0 -c:v hevc_vaapi -rc_mode CQP -qp 22 -map 0:a:0 -c:a eac3 -b:a 640k -ac 6 -map_metadata 0 -map_chapters 0 "/media/hevc-test/directplay-test.mkv"
```

In Plex add a library of type **Other Videos** pointing at
`/volume1/data/media/hevc-test` -- that shows files without needing to match
them to a show, and keeps the test away from the real libraries and the *arrs.
Play it on **both** TVs (the 1080p file on the 4K set too; different decoders,
either can fail). Delete the library and folder afterwards.

- **Direct Play on both** -> the last big assumption is confirmed. Proceed.
- **Direct Stream** -> container or audio remuxed, video untouched. Acceptable,
  but read the reason: it usually points at the audio choice.
- **Transcode (video)** -> stop, and reopen the codec decision.
- **Transcode (audio) only** -> the video plan survives; revisit `audio.target_codec`.

</details>

### 3. Fix `path_map` in the NAS `config.yaml` by hand -- DONE

**Written 2026-08-30**, whole file regenerated from `config.example.yaml`
rather than patched. What that run settled:

- **The NAS is `192.168.0.179`.** The example config's `192.168.1.10` is a
  placeholder on the wrong subnet -- it was never a real address here.
- The file is `root:root`; every later read of it needs `sudo`. That is the
  right state (it holds four credentials) and the container runs as root, so
  it reads it regardless.
- **Still FILL ME: `plex.token` and `tautulli.api_key`.** Phase 1 needs the
  Plex token for identity, so gather it before step 5.

The original reasoning, still correct:

Measured against the live instances: both *arrs report paths as **`/data/media/...`**,
their own container's view -- not the host path `/volume1/data/media/...` that
the old example config suggested. Nothing errors when this is wrong; *arr
matches simply never line up with our files and identity quietly falls back to
filenames. `config.example.yaml` is fixed, but the live `config.yaml` is
gitignored and was not touched:

```yaml
sonarr:
  path_map:
    "/data/media": "/media"
radarr:
  path_map:
    "/data/media": "/media"
```

The Sonarr and Radarr API keys that were used for the read-only run live in
`config/arr.local.yaml` **on the workstation**, which is gitignored. The NAS
`config.yaml` needs the same two keys.

### 4. Install the *arr guard, before any bulk encoding -- DONE

**Applied 2026-08-30** with `--apply --neutralise`. Nine writes: the custom
format created in both services, +1000 on six Sonarr profiles and two Radarr
ones, and Radarr's `x265 (HD)` raised -10000 -> 0 in `HD-1080p`. BR-DISK, MA
and Special Edition were left alone, which is the 08-29 detector fix holding.
A second `app arr-guard` reports **"already as it should be; nothing to
write"** on both, so it is idempotent and the writes stuck.

`notify_on_replace: true` is set for both services in the new `config.yaml`.
`app arr-rename` is still a step for *after* the first batch, and the three
`/command` names are still unposted -- the guard writes custom formats and
quality profiles, not commands.

The original reasoning, still correct:

Once encoding starts, Sonarr and Radarr will replace our HEVC files with fresh
H.264 downloads and delete ours to make room. The guard writes one custom
format matching HEVC and gives it a **positive** score on every profile.

Re-read the dry run first -- it changes with the library:

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app arr-guard
```

**Decided 2026-08-29: apply it with `--neutralise`.** Radarr scores its
`x265 (HD)` format at -10000, which would otherwise defeat the guard for every
film below 4K. That is now safe to do -- the detector no longer mistakes
TRaSH's BR-DISK for an HEVC rule, so `--neutralise` leaves the disc-image
penalty alone:

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app arr-guard --apply --neutralise
```

Every write is recorded in `guard_change`; `app arr-guard --revert` undoes it.

**Then turn on the rescan.** Set `notify_on_replace: true` for both services in
`config.yaml`. Without it the guard protects nothing we make: an *arr scores a
file by its *name*, our encode keeps the `[x264]` name, and the measured
coverage over encode candidates was **0 of 186 in Sonarr and 0 of 197 in
Radarr**. The naming formats already end in the video codec token and renaming
is already enabled, so the file only needs the *arr to re-read it. After the
first batch:

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app arr-rename
```

That prints the *arr's own preview of every new name and moves nothing until
`--apply`.

**Still unverified:** the three `/command` names (`RescanSeries`, `RescanMovie`,
`RenameFiles`) have never been posted, because posting is a write and the
session stayed read-only. Every read-only half is confirmed. A wrong name is
rejected and reported; it cannot damage anything.

### 5. Run Phase 1 on the real library -- capped run DONE, full sweep to go

**2026-08-30, `--probe-limit 200`.** The whole run took about four minutes:

```
seen 23287  added 23287  probed 200  probe failures 0
walk 132.0s  probe 40.4s
plex: 24707 matched  sonarr: 21412 matched  radarr: 1858 matched
resolved 23287/23287  (filename 11, plex 23272, sonarr 4)  unresolved 0
0 duplicate group(s)
```

**The library is 23,287 video files**, which replaces the guessed 6000 in every
timeline projection in this file.

**`path_map` is proven, not merely inspected.** 23,272 of 23,287 files resolved
through Plex and only 11 fell back to filenames. A wrong path map inverts that
ratio silently, so this is the evidence step 3 was waiting for.

**0 duplicate groups across 23,287 files.** Nothing is blocked by it -- `app
plan` only holds *unresolved* groups out of the queue -- but it is low enough
to be worth a glance if duplicate handling ever seems inert.

The full sweep is the same command without the cap. At the measured 0.20s per
probe, the remaining ~23,087 files should take **75-80 minutes**. Read-only,
resumable, writes only to our own SQLite:

```sh
cd /volume1/docker/vidsmasharr && nohup sudo docker compose -f docker/docker-compose.yml run --rm -T vidsmasharr app phase1 > ~/phase1-full.log 2>&1 &
```

Note the redirect goes to the home directory, not `/volume1/scratch`: with
`sudo` scoped to the docker binary only (see NAS access below), the shell
opening the redirect is the login user, who cannot write there.

### 6. Plan -- RE-RUN. First real jobs -- DONE, three bugs fixed on the way

**`app plan` ran twice on 2026-08-30**, the second time after the estimator
fix below changed every audio-derived number in it. These are the figures
that stand; anything quoting 4,814 jobs or 5,411 GB predates the fix.

| action | files | GB saved | encode-hours | GB/hour |
|---|---|---|---|---|
| encode | 4,629 | 4,977.1 | 2,855.3 | **1.7** |
| remux | 217 | 455.8 | **11.1** | **41.1** |
| downscale | 4 | 4.5 | 1.5 | 3.0 |
| **total** | **4,850** | **5,437.4** | **2,867.9** | |

**358 nights at 8h to finish everything** -- effectively a year. That is the
number that should shape strategy from here.

**But 456 GB is available in 11 hours.** The 217 remuxes are files already in
HEVC where re-encoding would make them bigger; the win is dropping surplus
audio tracks. 24x the GB/hour of encoding, and the queue is already ranked by
GB/hour, so `app work` reaches them first without being told. Every one of the
top 20 jobs is a remux, mostly multi-audio Ghibli rips shedding 7-12 tracks.
Against 3.8 TB free, two nights of remuxing is real headroom before a single
frame is re-encoded.

**The estimator fix barely touched this tier, and an earlier draft of this
file was wrong to warn that it would.** It guessed the remux headline might
fall from 461 GB to about 230. Measured: 455.8 GB. Almost all of the remux
saving is EAC3, AC3 and DTS tracks, whose guesses did not change; only AAC
did. What the fix really moved was *which* job is on top and what each one
individually promises -- not the aggregate.

The encode tier grew slightly, which is the same fix seen from the other
side: audio is subtracted from file size to get the video share, so a
smaller audio estimate means a larger video one, and 42 files that were
under the saving floor now clear it.

Why 17,639 files were not queued:

```
12238  below the minimum source size (700MB)
 2225  protected (HDR/DV/10-bit)
  888  no ladder rung for that resolution
  815  saving below the floor
  644  already at a low bitrate
  626  waiting on a duplicate decision
  203  already an efficient codec
```

The 626 unlock by working through the 150 duplicate groups that need a human.
The 12,238 are a policy choice (`min_source_bytes`), revisitable later.

**`app plan` reports GiB but labels it GB.** It printed 5,064 where the raw
bytes are 5,437.4 GB -- exactly a factor of 1.0737. Do not mix the two when
quoting figures. Same for its "1.8 GB per hour", and for the per-job GB in
`app work` output.

**`keepers_file /config/keepers.txt is configured but does not exist`** warns
on every run. Harmless -- keepers fall back to hardware -- but create an empty
file or blank the setting to quieten it.

Films are skipped with "no calibrated setting for movie at 1080p". Correct and
deliberate; they still get audio-only remuxes where those pay.

#### The first real job -- run 2026-08-30, and what it cost to get there

Both steps below have now been run. **Read the deadlock note before the
commands**; the second step does not work on any build older than `50c244b`.

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app work --limit 1
```

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app work --limit 1 --execute
```

The first prints the ffmpeg command. The second really encodes and verifies,
leaving the output in `/scratch/encoding` because
`delete_original_on_success` is off.

**The dry run was correct first time.** Top of the queue was the remux the
plan predicted: Kiki's Delivery Service, an AnimeRG rip shedding 12 audio
tracks and 10 subtitle tracks, video copied bit for bit.

**The execute run then hung for twelve minutes and had to be killed.** Not a
slow encode -- a deadlock, and one that would have hit the *first* job of any
future batch too:

- `ffmpeg` asleep in `pipe_wait`, zero CPU time, the output file never even
  opened. The parent `python3` asleep in `pipe_wait` as well.
- Cause: `_run_with_progress` piped stderr but only read it after the stdout
  loop reached EOF. ffmpeg dumps every stream to stderr before it opens the
  output, and a 26-stream source overruns the 64K pipe buffer doing it. It
  blocked writing the banner; we blocked waiting for progress that could no
  longer come.
- Worse, `timeout` was checked *inside* that blocking read, so the deadline
  could only fire on a process that was still talking. The one case it existed
  to catch was the one case it could not see. It would have hung all night.
- Fixed in `50c244b`: stderr goes to a temporary file, and a watchdog timer
  kills from outside the loop. Two regression tests; the deadlock one was
  confirmed to hang on the old code before being confirmed to pass on the new.

**The re-run succeeded in 72 seconds**, and the mid-flight recovery worked --
it reported `recovered 1 decision(s) left mid-flight by an interrupted run`
and picked the killed row back up by itself. 3,028,272,347 bytes in,
1,810,016,666 out.

#### Then the output turned out to be wrong, and the estimate with it

That first output was inspected before anyone watched it, which is the only
reason either of the next two bugs was found. **Look at what comes out, not
just at whether the run said "succeeded".**

**It had lost every font.** The source carries 15 TTF attachments; the output
had none. Both subtitle tracks kept are ASS, which is the format that styles
signs and songs *by reference* to those attachments -- so the typesetting
would have fallen back to whatever the player had. Nothing in the code
mentioned attachments at all, and nothing in the output mentioned them either,
which is how it went unnoticed. Fixed in `2561f41`: `-map 0:t?` wherever a
subtitle survives, and the dry-run summary now says "fonts kept". The `?`
matters -- most files have no attachments and ffmpeg must not fail on them.
Both cases were verified against real media on the NAS before the code was
written.

**And the 2x estimate gap was arithmetic, not mystery.** mkv stores no
per-track bitrate, so `track_bitrate` fell back to a flat guess of 256k for
every AAC track. Twelve dropped tracks times 256,000 times 6184.66 seconds is
2.21 GiB -- the estimate, to the digit. The real tracks run near 128k.

The same constant corrupted the other side too, and that is the part worth
remembering: `source_video_bytes` is *file size minus audio*, so over-stating
audio under-states video. It had concluded that 13 AAC tracks were 2.57 GB of
a 3.03 GB file, leaving 455 MB of video for a 103-minute 1080p HEVC feature,
and predicted a 660 MB output where 1.81 GB landed.

Fixed in `e373bb0`: AAC is guessed at 64k per channel -- 128k stereo, 384k for
5.1, which the same file's Italian and Spanish tracks genuinely are. Unknown
channel count falls back to stereo rather than to something larger, keeping
the error on the under-promising side the module is built to err on.

#### What the two real jobs actually measured

Both re-run after all three fixes, on 2026-08-30:

| file | before | after | saved | estimated | |
|---|---|---|---|---|---|
| Look Back (2024) | 4,472,477,259 | 1,642,583,821 | 2.64 GiB | 2.64 GiB | exact |
| Kiki's Delivery Service | 3,028,272,347 | 1,815,850,428 | 1.13 GiB | 1.46 GiB | 30% over |

Look Back landing exactly is a fair test of the fix rather than a lucky one:
its dropped tracks are EAC3, whose guess never changed, so it shows the AAC
change did not disturb what already worked. Kiki's residual 30% is the part
still unexplained -- subtitle and font bytes the model does not account for,
plus tracks genuinely below 128k. Still over-promising, which is the wrong
direction for this module. Worth another look once more outcomes exist.

**CPU is under-estimated by roughly 1.7x on both** (49.6s against 28.4s,
32.4s against 19.3s). Small in absolute terms for a remux, but it feeds the
GB/hour ranking. That is what `app calibrate` is for, after a batch.

Kiki also proved the font fix end to end: 15 attachments in the output, about
5 MB. Look Back correctly has none -- its source has none, only 41 SubRip
tracks, which need no fonts. That incidentally tested the `?` on a real file
with no attachments.

#### The database surgery that redoing them needed

The first, fontless output had already been verified and recorded. Before
re-running, on 2026-08-30 (DB backed up first to
`/volume1/scratch/vidsmasharr.db.pre-replan.bak`, 45 MB):

- the stale `outcome` row was deleted, so calibration never sees a run whose
  command differs from what the code now produces
- both `job` rows for the decision were set to `state='superseded'` rather
  than deleted. This keeps the deadlock on the record while keeping them out
  of the `j.state = 'done'` join in `install_held` -- **leaving a second
  `done` job row against one decision would have made `install_held` try to
  install the same file twice.** Worth knowing before hand-editing this table
  again; `job.state` has no CHECK constraint, so a new value is safe.
- the decision went back to `state='pending'`, and the fontless file was
  deleted from scratch

Then `app plan` was re-run, *after* the reset so the redone job got a fresh
estimate too. Order matters: a decision still in `held` is not re-planned.

#### Where the outputs are, and why you cannot see them

Both live in scratch, which is where `held` outputs wait:

```
/volume1/scratch/vidsmasharr/encoding/
```

**That path is unreachable by anyone but root.** `/volume1/scratch` is
`drwx------ root:root` -- created by Docker, not through DSM's share manager,
so unlike every other top-level folder on `/volume1` it has no DSM ACL, is not
an SMB share, and Plex cannot read it either. The files inside are `644`; it
is the directory above them that blocks everything.

So copies were placed where Plex can reach them, `docker:users` `644` to match
the rest of the library:

```
/volume1/data/media/vidsmasharr-test/
```

It is a sibling of `Anime/`, `movies/` and `tv/`, deliberately **not** inside
any *arr root folder, so nothing will try to import or rename them. Add it to
Plex as an **Other Videos** library -- that skips metadata matching, which a
Movies library would attempt and probably fail on the AnimeRG filename.

**Delete that folder when the testing is done.** It is 3.4 GB of duplicate,
and the scratch copies are the ones `app work --install-held` needs -- do not
delete those instead.

#### What to check on the TVs -- CHECKED 2026-08-31, both good

**Both files were watched on both TVs on 2026-08-31 and passed.** That closes
the last thing standing between the pipeline and the library: the outputs are
confirmed good on the hardware that has to play them, not merely confirmed to
have been produced. Next is `delete_original_on_success` on and
`app work --install-held`, then delete `/volume1/data/media/vidsmasharr-test/`.

What was checked, and what happens after:

- **Direct Play, not transcode**, on both. That is the assumption the whole
  ladder rests on.
- **Kiki:** signs and songs should be *styled*, not plain -- that is the font
  fix. English audio only, two English subtitle tracks.
- **Look Back:** EAC3 5.1 English, 12 English subtitle tracks, no fonts.

Then turn `delete_original_on_success` on and use `app work --install-held`,
which installs what is already in scratch rather than spending the work again.

### 7. Only after a real batch: calibrate

```sh
cd /volume1/docker/vidsmasharr && sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app calibrate
```

Reports and changes nothing until `--apply`. It needs 8 outcomes per model
before it will produce a factor, so this is a step for after the first batch,
never before. Re-run `app plan` afterwards -- the queue order is the point.

### Do not re-litigate these

- Anything not provably 8-bit SDR is never rewritten. No exception for "the
  *arr says it is fine".
- **The guard's score is positive**, and that is not a typo. Both *arrs compare
  a candidate release's score against the score of the file already on disk, so
  a high score on HEVC is what protects it. A negative score would make every
  H.264 release an upgrade over a night of work. There is a test named for it.
- A dry-run diff before writing to Sonarr/Radarr is locked; there is no flag to
  skip it.
- **Kept subtitles keep their fonts.** `-map 0:t?` is not decoration: an ASS
  track styles signs and songs by reference to the container's attachments, so
  a remux that drops them silently degrades the track it just chose to keep.
  Found the hard way 2026-08-30; see section 6.
- `app/identity/arr.py` stays read-only. Write verbs live in `app/guard/`.
- The ladder groups by content class. Do not "simplify" that back to
  (encoder, resolution) -- see the session 4 notes for what it cost.
- Every backgrounded command in this file is in the `sudo sh -c '...'` form
  because the redirect must be opened by root. Do not simplify one back.
- **VMAF still aggregates by minimum in the default path, and `--robust` is a
  flag rather than the new default.** Both are deliberate. The min rule is
  correct when the clips are a homogeneous sample; it is what breaks when they
  are not, and only measured outcomes should decide which regime the real
  library is in. Do not flip the default before `app calibrate` has a reject
  rate to show.
- **A size ratio is only comparable within one rung.** Under `--robust` two
  encoders can survive different clips, so the printed ratios are averages over
  different populations. That is what the clips column is for.

---

## Session 6 (2026-08-30): the box says yes

Four things happened on the real hardware, and none of them was code for its
own sake.

**Direct play passed on both TVs.** See step 2 of the brief. That was the last
assumption the whole plan rested on.

**The NAS `config.yaml` was written for the first time**, whole rather than
patched, from `config.example.yaml`. It carries the real Sonarr and Radarr
keys, the correct `path_map`, and `192.168.0.179` -- the example's
`192.168.1.10` was a placeholder on a subnet this network does not use. Two
values are still blank and marked FILL ME: `plex.token` and
`tautulli.api_key`.

**The guard was applied.** First writes this project has ever made outside
itself. Idempotent on the second pass.

**One real bug fell out of it, and it was not in the guard.** `app arr-guard`
began timing out on calls that had worked twenty minutes earlier. Timing them
with curl from the NAS shell settled it in one step: Sonarr's `/series` takes
46 seconds here, Radarr's `/movie` 27, and the client's timeout was hardcoded
at 30. Sonarr could never have worked; Radarr worked about half the time,
which is why the first run passed and the second did not. `ArrConfig.timeout`
now exists, defaults to 180s, and is passed at both construction sites --
including `load_sonarr`/`load_radarr`, which Phase 1 uses on every run and
which would have hit the same wall at step 5 with nothing to blame.

**A warning written the same day and then disproved, recorded so nobody
reinstates it.** Reading `SonarrClient.matches()`, the assistant predicted that
its two-calls-per-series walk would make Phase 1 look hung. It did not: the
capped Phase 1 run finished in about four minutes, identity included. The 180s
timeout was still the right fix -- Sonarr's `/series` really does take 46
seconds -- but the per-series walk is not slow on this box.

**Tautulli was enabled, and the key verified rather than assumed.** The
distinction matters here specifically, because a bad key produces the same
observable result as a good one (trap 10). The check is Tautulli's own
`result` field:

```
tautulli HTTP 200 | result: success
tautulli_streams() -> 0
someone_is_watching() -> (False, 'Tautulli reports nobody watching')
```

`result: success` is the proof; `stream_count: 0` on its own is not.

**Phase 1 ran on the real library for the first time**, then in full. 23,287
files, 23,078 probed in 47 minutes, all resolved. See step 5.

**A correction worth keeping:** the capped run reported *0 duplicate groups*,
and that was an artifact of the 200-probe cap, not a property of the library --
duplicate detection needs probe data. The full sweep found **238 groups over
684 files, 166 GB reclaimable, 150 needing a human**. Never read a duplicates
figure from a capped scan.

**9 files cannot be probed at all**, and they are genuinely broken rather than
a scanner bug: truncated MP4s (`moov atom not found`) and MKVs whose EBML
header starts with a zero byte. Plex cannot play them either. They are
correctly excluded -- unprobeable means unplannable. **One small defect this
exposed:** the scanner stores `ffprobe failed (1) for <path>:` and **discards
ffprobe's stderr**, so the reason has to be re-derived by hand. Capturing
stderr into `probe_error` is a few lines and has not been done.

**`app plan` ran**, and its numbers are in step 6. The short version: a year of
encoding to reclaim 5.4 TB, with 456 GB of it sitting in 11 hours of remuxes
that the queue already ranks first.

**The assistant was given SSH plus passwordless docker on the NAS**, which is
how the second half of the day got done. See "Driving the NAS from the
workstation" near the top, including how to revoke it.

The NAS tree is now hand-patched with files from `ladder-robust` (since merged
into `main`). This paragraph and the table near the top of the file once named
two different sets of four; **the md5 sweep of 2026-08-31 settled it** -- the
tree and the image are `485c74c` throughout, bar four inert test files. See
"Git on the NAS does not work".

---

## Session 5 (2026-08-29): the wider benchmark, and what it broke

Branch `ladder-robust`, 336 tests. Not merged to `main` at the time -- it
changes how every rung is chosen and deserved a real batch behind it first.
**It was merged afterwards and the branch deleted; `main` at `485c74c` carries
all of it.**

```
bench/ladder.py    + --robust: per-clip interpolation, second-hardest rung,
                     clips set aside by name
                   + size-disagreement warning (only VMAF was warned about)
                   + a clips column, so rungs from different populations
                     cannot be compared by accident
                   + --run-class now persists the label into bench_result
```

Three bugs found on the way, all in code that predates this session:

1. **`--run-class` labelled in memory only.** Every later rebuild had to
   remember the flag, and forgetting was silent *and* expensive: an unlabelled
   run falls into the `unknown` class, and an unknown group is allowed to speak
   for **every** target -- so six TV clips would have quietly produced movie
   rungs, the exact fabrication the content classes exist to prevent. The label
   is now written into the measurements.
2. **The wide-VMAF-spread branch assigned to `note` instead of appending**,
   silently discarding the excluded-broken-clip and unknown-content-class
   notes whenever it fired. Notes accumulate in a list now.
3. **Size disagreement was never warned about**, only VMAF disagreement. At
   720p two clips of the same show differ by 49 points of size at the chosen
   setting, and the rung reported their average as though it described either.

And one introduced and then fixed in the same session: `--robust` made the
cross-encoder size comparison invalid, because each encoder sets aside its own
clips. The clips column exists because of that.

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

316 tests pass.

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

### The pipeline has now been run for real, on a workstation

Every Phase 3 test mocks ffmpeg out, so until now no part of the encode path
had ever had a real binary on the other end of it. This workstation turns out
to have ffmpeg 8.1.2 with libvmaf, libx264 **and** libx265, which is enough to
run the whole thing end to end with libx265 standing in for hevc_vaapi.

Three synthetic 1080p H.264 files (90s, 8 Mbps, noise-heavy, eng+fra audio) and
one already-HEVC file, then `app phase1` -> `app plan` -> `app work --execute`
-> `app work --install-held` -> `app calibrate`. It worked:

- the planner refused the HEVC file for re-encode and caught it as a remux to
  drop the French track, which floated to the top of the queue on GB-per-hour
  exactly as designed
- real encodes, real libvmaf verification (93.1 and 93.2 against a target of
  92), real atomic swap, real deletes, and the database adopted the new files
- the outcome rows survived the decision being deleted, which is the migration
  7 fix proving itself outside a test

**It also found two bugs, which is the point.**

1. **Three jobs produced six outcome rows.** `run_decision` records an outcome
   when the encode verifies, and `install_held` recorded a *second* one when it
   later installed the held output. That doubles the reclaimed total on
   /activity and hands the estimator two votes per file -- and the duplicate
   carries no encode time and no VMAF, because neither can be measured once the
   source has been replaced. Installing is the end of the first job, not a
   second one, so it now updates the existing row. There is a regression test.
2. **`--min-samples` was honoured when building the correction but ignored by
   the report**, which still printed "(too few)" for groups it had just used.
   The threshold now travels on the group.

While there: the /activity "actually reclaimed" tile counted verified-but-not
-installed work as reclaimed. During a trial batch nothing has been freed yet,
so it now shows what is really back on the volume and notes the rest separately.

### One artifact of the test rig, not a bug

The encoded outputs came out at 43 MB, under the walker's 50 MB
`MIN_USEFUL_BYTES` floor, so the next scan did not see them and marked them
missing. The vanish guard did not fire because it deliberately ignores roots
with fewer than 20 known files. Neither can happen on the real library --
`min_source_bytes` is 700 MB and ratios are around 40%, so outputs are hundreds
of megabytes -- but it is worth knowing that a file which shrinks below 50 MB
would lose its identity on the next walk.

### The guard has now met the real Sonarr and Radarr (read-only)

Run from the workstation over the LAN, no `--apply`. Sonarr 4.0.19.2979 and
Radarr 6.3.0.10514, both reachable, both offering `ReleaseTitleSpecification`.
This answered the open question and turned up a bug that would have done real
damage.

**The detector for other people's HEVC formats was too loose, and its false
positive was dangerous.** It tested whether a format's regex *text* contained
"265" or "hevc", and on the real Sonarr that flagged TRaSH's **BR-DISK** at
-10000. BR-DISK names HEVC only inside a *negative lookahead*: it is a rule
about full BluRay disc images that deliberately excludes HEVC. `--neutralise`
would have raised it to zero and let both apps start grabbing 40GB disc rips.
It also flagged an anime release-group list, on a group whose name contains
those digits.

The test is now semantic: run the pattern against two release names differing
only in codec, and call it an HEVC rule if it matches the HEVC one and not the
H.264 one. Patterns Python cannot compile -- .NET allows variable-length
lookbehind and BR-DISK uses one -- are reported as "could not evaluate" rather
than guessed at in either direction. Guessing "yes" is the answer that does
damage. The real BR-DISK and x265 (HD) patterns are in the tests.

**The one genuine conflict: Radarr scores `x265 (HD)` at -10000.** Its pattern
is `[xh][ ._-]?265|\bHEVC(\b|\d)` with a negated 2160p resolution spec, so it
penalises HEVC at everything below 4K -- most of the film library. While that
stands, nothing the guard adds protects a film. Sonarr has no such format.

**Coverage: 100%, and the 100% was meaningless.** Of 14 HEVC files sampled in
Sonarr the format matched 14. But those files are HEVC because they were
*downloaded* as HEVC releases, so of course their names say so. The number that
predicts anything is taken over the H.264 files -- the ones we would re-encode,
whose names our output inherits -- and that came back **0 of 186 in Sonarr and
0 of 197 in Radarr**. The guard as it stands would protect essentially none of
our own work. The report now leads with that figure instead.

### ...and the fix is much cheaper than expected

The reason is not that the naming format lacks the codec. Read from the live
instances:

- Sonarr `standardEpisodeFormat` ends with `{[MediaInfo VideoCodec]}`, and so
  do the daily and anime formats. Radarr's `standardMovieFormat` ends with
  `[{Mediainfo VideoCodec}]`.
- `renameEpisodes` and `renameMovies` are both **already true**.
- **0 of 200 Sonarr files and 199 of 200 Radarr files have no `sceneName`**, so
  scoring falls back to the file name -- which is the one carrying the codec.

So every filename already ends in `[x264]`, `[XviD]` or `[x265]`, and the
existing HEVC files render as `[x265]` (x265-encoded) or `[HEVC]` (codec
`h265`, which is what our hardware VAAPI encode will produce). The guard's own
regex matches all three spellings.

**The missing link is therefore a rescan, not a rename scheme.** After we
replace a file, the *arr still believes it is x264 until it re-reads the
file's media info; once it does, its own renaming turns `[x264]` into `[HEVC]`
and the custom format matches. Nothing in the project triggers that today.

That is the next thing worth building, and it is deliberately not built yet:
it writes to the *arrs (`POST /command`) and it renames files in the library
that Plex has already indexed. Both are the user's call.

### Session 4, later: the rescan step, and two more findings

Decided with the user and built: the worker can now tell the owning *arr that a
file changed (`notify_on_replace`, off by default), and `app arr-rename` shows
the *arr's own rename preview and applies it only when asked. Rescan is
automatic because it changes nothing on disk; renaming is not, because it
changes filenames Plex has indexed.

Two things came out of pointing it at the live instances:

- **The documented `path_map` was wrong.** `config.example.yaml` mapped
  `/volume1/data/media`, the NAS host path. Both *arrs actually report
  `/data/media/...`, their own container's view. Nothing errors with the wrong
  map -- *arr matches just never line up with our files, and Phase 1 identity
  silently falls back to filenames. Fixed in the example; a `config.yaml`
  already written on the NAS needs the same change by hand. There is a test
  named for this failure mode.
- **Sonarr's rename preview is slow enough to time out.** `GET /rename` is
  computed server-side over every file in the series and blew past the 30s
  default on a six-season show. That one call now gets 180s; the rest keep the
  normal timeout.

Radarr's preview was verified live and returns exactly the expected shape --
`A.Dog's.Way.Home.2019.720p.BluRay.x264-[YTS.AM].mp4` becomes
`A Dogs Way Home (2019) {imdb-tt7616798} [Bluray-720p][AAC 2.0][x264].mp4`,
which is the naming format doing its job. After a re-encode and a rescan the
last bracket becomes `[HEVC]`, and the guard's format matches it.

**The command names are still unverified.** `RescanSeries`, `RescanMovie` and
`RenameFiles` are posted to `/command`, and posting is a write, so none of them
has been tried against the live instances. The read-only halves -- listing
items, locating a file by folder, previewing renames -- are all confirmed. If a
command name is wrong the *arr rejects it and the error is reported; it cannot
damage anything.

### The ladder as rebuilt on 2026-08-29

From the one surviving run, labelled TV. No warnings left -- every rung is
bracketed by real measurements:

| encoder | class | res | target | setting | size | fps |
|---|---|---|---|---|---|---|
| hevc_vaapi | tv | 1080p | 92 | qp=26 | **15%** | 37.5 |
| hevc_vaapi | tv | 720p | 92 | qp=24 | 33% | 38.8 |
| hevc_vaapi | tv | sd | 92 | qp=24 | 50% | 168.5 |
| hevc_qsv | tv | 1080p | 92 | gq=22 | 26% | 27.9 |
| hevc_qsv | tv | 720p | 92 | gq=22 | 54% | 29.4 |
| hevc_qsv | tv | sd | 92 | gq=23 | 56% | 116.4 |

**vaapi beats qsv everywhere**, on both size and speed, settling the encoder
question again with cleaner numbers. 37.5 fps is about **29 minutes for a
45-minute episode**.

**No movie rungs at all, which is correct.** `app plan` will skip films rather
than queue them against a fabricated ratio.

Two caveats recorded with it:

- **The 1080p rung is two clips from one show.** See step 1 of the brief.
- **At 720p the two Alex Rider clips disagree on *size* by 5x** (12.9% vs 62.1%
  at the same setting). VMAF disagreement is warned about; size disagreement is
  not. The 33% is an average of two very different scenes, so 720p savings
  estimates are softer than the 1080p ones. Possible future warning.

### The movie run's measurements were lost

`bench.ladder --list-runs` on 2026-08-29 found **one** run: 60 measurements,
six TV clips. The movie run's 24 measurements (Swapped x2, Pitch_Black x2, at
three quality points across two encoders) were present in a ladder built an
hour earlier and gone afterwards.

Cause: the old rebuild one-liner backs config up with
`cp -a vidsmasharr/config /volume1/scratch/vidsmasharr-config.bak`, and **if
that directory already exists, `cp -a` copies *into* it** rather than over it.
The restore step then pulls the older session-3 database back out. Anyone using
that command must delete the backup directory first or use a dated name.
(It was described as retired "since the NAS has Git". Git does not work on the
NAS -- see the git section below -- so this command is NOT retired.)

**The loss costs almost nothing.** Both movie clips were already established as
invalid: `Swapped (2026)` is a modern Netflix WEB-DL that inflates at every
setting, and `Pitch_Black` clip 1 was a broken comparison (75-78 VMAF across
the whole sweep while its output ran to 1.9x the source). Recovering them would
only let the ladder print "movie: unusable" from real rows instead of from
absence.

### What Phase 4 has not met

The guard has never *written* to the real Sonarr or Radarr -- the run above was
read-only, and `--apply` has not been used. Nothing in `guard_change` yet.

Calibration has run against genuinely produced outcome rows on the workstation,
never against NAS hardware, so its factors mean nothing for the DS1019+ yet.

---

## Where we are

**Phase 0 is complete: the calibration ran on 2026-08-28 and hevc_vaapi won
outright, and direct play was confirmed on both TVs on 2026-08-30. No
assumption about playback is left standing between here and bulk encoding.**

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
- `main`, local and remote in sync at `e842d30` (that was then; `485c74c` now)
- **Phases 0-4 are all built**, 323 tests. What remains is running them against
  the real box -- see the top of this file
- **The NAS cannot run git.** The claim that it is a working checkout was
  wrong; see "Git on the NAS does not work" below for what to do instead
- A movie calibration was still running when session 3 ended; its result is
  still unknown
- 293 tests passing (Phase 1 added 72, Phase 2 added 45, Phase 3 added 60,
  the ladder fix added 7, Phase 4 added 61)
- **Phase 3 has now been run end to end against real ffmpeg and real libvmaf**
  on a workstation, with libx265 standing in for hevc_vaapi. It works, and it
  turned up two real bugs -- see session 4 below
- **Nothing has touched the real media library yet.** Phase 3 now *can* -- it
  is the first code that deletes -- but it ships with `dry_run` on and
  `delete_original_on_success` off. It has now been run for real against a
  synthetic library on a workstation; it has never been run against the NAS.

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
8. **`/usr/local/bin` is not on the login user's `PATH`.** It is
   `/usr/bin:/bin:/usr/sbin:/sbin`, and DSM symlinks package binaries --
   `docker`, `docker-compose` -- into `/usr/local/bin`. So a binary can look
   absent to an interactive shell and work fine under `sudo`, which has its own
   `secure_path`. `visudo` is missing from that `secure_path` in turn. Check
   `ls -l /usr/local/bin/` before concluding anything is not installed.
9. **An *arr that is slow is not an *arr that is down.** Sonarr's `/series`
   takes **46s** on this box (2.6MB) and Radarr's `/movie` **27s** (11.6MB) --
   both whole-library calls. The client's 30s timeout was hardcoded and
   reported the overrun exactly like an unreachable host. Fixed 2026-08-30
   (`ArrConfig.timeout`, default 180s). If an *arr call ever "times out",
   time it with curl before believing it.
10. **An empty Tautulli `api_key` does not disable Tautulli.** It answers a bad
   key with HTTP 200 and an error payload, which parses as **0 streams**, so
   `pause_when_streaming` silently decides nobody is watching. Set
   `tautulli.enabled: false` until the key is filled in -- that path returns
   None and falls back to Plex.
11. **The rebuild command deletes the database.** `../config:/config` means
   the SQLite file, `profiles.yaml` and `config.yaml` all live inside the repo
   directory that `rm -rf vidsmasharr` removes. Losing it costs the whole
   calibration run and a full re-scan. Back `config/` up first -- see the
   rebuild command above -- or install Git and stop deleting the tree at all.
8. **`sudo cmd > /volume1/scratch/...` fails with "Permission denied."**
   **Hit twice now, once on 2026-08-29 from a command written straight past
   this very note.** Every backgrounded command in this file is written in the
   `sudo sh -c '...'` form for that reason -- do not "simplify" one back.
   The shell opens the redirect as the *login user* before `sudo` ever runs,
   and scratch is root-owned. Put the redirect inside root:
   `sudo sh -c 'nohup docker compose ... > /volume1/scratch/vidsmasharr/bench.log 2>&1 &'`
   The job number prints and then the job dies, so it looks like it started.

---

## Locked decisions (from a long Q&A — do not reopen)

| Area | Decision |
|---|---|
| TVs | One 4K + one 1080p, both smart TVs 2018+, HEVC direct play **verified on both 2026-08-30** |
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

Everything above passes on synthetic data with ffmpeg mocked out. **Two real
remuxes have now been through the pipeline** (2026-08-30, see "The first real
job" near the top). Between them they found three bugs that the whole mocked
suite had missed, and the reason is worth keeping in mind when adding tests
here: mocking `_run_with_progress` mocks out the deadlock, and no test looked
at the streams in a produced file, so no test could see the fonts go missing.

Still unproven on real media: **any re-encode at all**, VMAF verification
against a real source, the atomic swap, and deletion. Everything measured so
far is stream-copy work.

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

~~**Direct play has not been verified on either TV.**~~ **Verified 2026-08-30:
both TVs report Direct Play.** See step 2 of the brief above.

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
   sudo sh -c 'cd /volume1/docker/vidsmasharr && nohup docker compose -f docker/docker-compose.yml run --rm -T vidsmasharr bench --libraries /media/tv /media/movies --sources 4 --keep-clips > /volume1/scratch/vidsmasharr/bench.log 2>&1 &'
   ```
   What matters in the output: **fps on 1080p H.264** (replaces the invalid
   35.5 figure) and **size ratio at the chosen QP** (validates the 40-60%
   reduction assumption the whole 8-12 TB estimate rests on).

   </details>
4. ~~**Verify direct play.**~~ **Done 2026-08-30 -- Direct Play on both TVs.**
5. **Run the plan for real**, once the calibration has written
   `config/profiles.yaml`:
   ```sh
   sudo docker compose -f docker/docker-compose.yml run --rm vidsmasharr app plan
   ```
   It refuses to run without a calibrated ladder; `--provisional` shows the
   shape of the plan anyway, in a state Phase 3 cannot execute. Run
   `app duplicates` first -- files in an unresolved duplicate group are
   deliberately held out of the queue.
6. ~~**Run one real job.**~~ **Done 2026-08-30 -- two remuxes, 3.76 GiB, held
   in scratch.** It took three fixes to get there (`50c244b` deadlock,
   `2561f41` fonts, `e373bb0` estimator) -- read section 6 before running
   anything. A real *re-encode* still has not run. The commands:
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

- **Install Git on the NAS -- STILL OPEN, and it was wrongly marked done.**
  This was recorded on 2026-08-29 as finished: Git Server from Package Center,
  the tree converted in place with `git init` + a remote + `git reset --hard
  origin/main`, and `sudo git pull` as the one-line update. Later the same day
  `git` turned out not to be on `PATH` at all, so none of those commands run.
  See "Git on the NAS does not work" near the top for the evidence and for the
  curl-and-rebuild pattern used instead.

  Whatever the cause, the lesson is the one this file exists for: **a step is
  not done until it has been run on the box.** The `git init` conversion was
  written up from intent, not from a successful `git pull`.
- ~~**Plex token, Sonarr URL/key, Radarr URL/key.**~~ **All gathered
  2026-08-30**, along with the Tautulli key, and proven working by the Phase 1
  run. They live in the NAS `config/config.yaml`, which is **gitignored** --
  never in `config.example.yaml`.
- ~~**Is Tdarr still active?**~~ **Answered 2026-08-28: no Tdarr container is
  running.** `docker ps` shows 26 containers and none is Tdarr, so the
  `@tdarr-ffmpeg` core dump is historical. Nothing else is competing for
  `/dev/dri`. Still worth a glance at Package Center in case a stopped
  container has a schedule attached.
- ~~**Sonarr confirmed?**~~ **Answered 2026-08-28: yes**, `sonarr` is up on
  :8989 (`lscr.io/linuxserver/sonarr`). Radarr :7878, Prowlarr :9696,
  Bazarr :6767, Lidarr :8686 also confirmed running.
- ~~**Library file counts** unknown.~~ **Answered 2026-08-30: 23,287 video
  files** across `/media/tv`, `/media/movies` and `/media/Anime`. That is the
  figure that replaces the guessed `6000` in every timeline projection in this
  file -- roughly 3.9x, so any hour estimate derived from 6000 is badly low.
- **Is VMAF 95 reachable for movies on this hardware?** *Still unanswered, and
  the two attempts so far could not answer it.* Both movie calibration sources
  were invalid: `Swapped (2026)` is a modern Netflix WEB-DL already too
  efficient to beat (it inflates at every setting), and `Pitch_Black` clip 1
  was a broken comparison. **Do not sweep lower on sources like those** -- it
  only makes bigger files. The next movie calibration needs a **fat H.264
  BluRay rip at 8-15 Mbps**, the kind the planner would actually queue. The
  census says 91% of library bytes are H.264, so such files exist in quantity;
  the sampler just did not pick one. Only once a valid source has been measured
  is "lower `quality.movie_vmaf`" a decision worth taking.
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
