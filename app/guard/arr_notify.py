"""Tell Sonarr and Radarr that a file changed under them.

The guard writes a custom format that matches HEVC in the release title, and an
*arr scores a file by its *name*. Our encode replaces the bytes and keeps the
name, so the moment after a successful swap the *arr still believes the file is
x264 -- and the format it was just given cannot match anything.

On the instances this was written against, closing that gap is cheap, because
the naming format already ends in the video codec token and renaming is already
switched on. The file only needs the *arr to look at it again.

That is two separate actions with very different weight, and they are kept
apart on purpose:

- **Rescan** asks the *arr to re-read the file and update its own database.
  Nothing on disk changes. This is safe enough for the worker to do by itself
  after each replacement, and it is what makes the rename possible.
- **Rename** changes filenames in the library that Plex has already indexed.
  That is never automatic. `app arr-rename` shows the *arr's own preview of
  every new name and does nothing else until `--apply`.

A failure anywhere here is reported and swallowed. By the time this runs the
encode is verified and installed; an unreachable Sonarr is a reason to warn,
never a reason to fail a job that already succeeded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.identity.arr import ArrUnavailable
from app.identity.plex import map_path
from app.guard.arr_guard import ArrWriter, client_for

# How long to wait for a rescan to finish before giving up on it. The *arr runs
# commands asynchronously and a rename started too early would rename the file
# to the name it already has, using media info from before we touched it.
RESCAN_TIMEOUT_S = 120
RESCAN_POLL_S = 2.0

# The rename preview is computed server-side over every file in the item, and
# on a long-running series that takes Sonarr well past the 30s default -- it
# timed out on a 6-season show the first time this was pointed at a real
# instance. Only this call is slow; the rest stay on the normal timeout.
RENAME_PREVIEW_TIMEOUT_S = 180.0

# Per service: the command that re-reads one item, the command that renames its
# files, and the query parameter both are addressed by.
SERVICE_COMMANDS = {
    "sonarr": {
        "kind": "series",
        "rescan": "RescanSeries",
        "rename": "RenameFiles",
        "id_field": "seriesId",
        "list_endpoint": "series",
    },
    "radarr": {
        "kind": "movie",
        "rescan": "RescanMovie",
        "rename": "RenameFiles",
        "id_field": "movieId",
        "list_endpoint": "movie",
    },
}


@dataclass
class Item:
    """One series or movie, as the *arr knows it."""

    service: str
    item_id: int
    title: str
    folder: str          # already mapped into our view of the filesystem


@dataclass
class RenameProposal:
    service: str
    item: Item
    file_id: int
    existing_path: str
    new_path: str

    @property
    def summary(self) -> str:
        return (
            f"{Path(self.existing_path).name}\n"
            f"           -> {Path(self.new_path).name}"
        )


@dataclass
class NotifyResult:
    rescanned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _folders(client: ArrWriter, service: str, path_map: dict) -> list[Item]:
    """Every item's folder, mapped into our paths. One call, cached per client.

    Matching a file to its series by folder prefix avoids keeping a path->id
    index of our own, and avoids one API call per series to find out which one
    owns a file we already know the path of.
    """
    cached = getattr(client, "_vidsmasharr_folders", None)
    if cached is not None:
        return cached

    spec = SERVICE_COMMANDS[service]
    items = []
    for entry in client._get(spec["list_endpoint"]) or []:
        folder = entry.get("path")
        if not folder or entry.get("id") is None:
            continue
        items.append(Item(
            service=service,
            item_id=entry["id"],
            title=entry.get("title") or "?",
            folder=map_path(folder, path_map).rstrip("/\\"),
        ))
    client._vidsmasharr_folders = items
    return items


def locate(client: ArrWriter, service: str, path: str | Path, path_map: dict) -> Item | None:
    """Which series or movie owns this file, or None if this *arr does not.

    Longest folder first, so a nested library layout resolves to the innermost
    item rather than whichever happened to be listed earliest.
    """
    target = str(path).replace("\\", "/")
    best: Item | None = None
    for item in _folders(client, service, path_map):
        folder = item.folder.replace("\\", "/")
        if target.startswith(folder + "/"):
            if best is None or len(folder) > len(best.folder):
                best = item
    return best


# ------------------------------------------------------------------ commands


def _run_command(client: ArrWriter, payload: dict) -> int | None:
    response = client.post("command", payload)
    return (response or {}).get("id")


def _await_command(client: ArrWriter, command_id: int, timeout: float) -> str:
    """Block until the *arr says the command finished. Returns its final state.

    Worth the wait: a rename issued before the rescan lands would compute the
    new name from the media info we just invalidated, and rename the file to
    exactly what it is already called.
    """
    deadline = time.monotonic() + timeout
    state = "unknown"
    while time.monotonic() < deadline:
        try:
            body = client._get(f"command/{command_id}") or {}
        except ArrUnavailable:
            return "unreachable"
        state = body.get("status") or body.get("state") or "unknown"
        if state in ("completed", "failed", "aborted", "cancelled"):
            return state
        time.sleep(RESCAN_POLL_S)
    return f"still {state} after {timeout:.0f}s"


def rescan(client: ArrWriter, service: str, item: Item, *, wait: bool = True) -> str:
    """Ask the *arr to re-read one item. Returns what happened, for the log."""
    spec = SERVICE_COMMANDS[service]
    command_id = _run_command(
        client, {"name": spec["rescan"], spec["id_field"]: item.item_id}
    )
    if command_id is None:
        return "accepted (no command id returned)"
    if not wait:
        return "started"
    return _await_command(client, command_id, RESCAN_TIMEOUT_S)


def notify_replaced(config, path: str | Path, *, progress=None) -> NotifyResult:
    """Tell whichever *arr owns this path that it changed.

    Called by the worker straight after a successful install. Never raises: the
    file is already in place and verified by this point, and no failure here
    should turn a finished job into a failed one.
    """
    result = NotifyResult()
    for service in ("sonarr", "radarr"):
        settings = getattr(config, service, None)
        if not settings or not getattr(settings, "notify_on_replace", False):
            continue
        client = client_for(service, config)
        if client is None:
            continue
        try:
            item = locate(client, service, path, settings.path_map)
            if item is None:
                continue
            state = rescan(client, service, item)
            result.rescanned.append(f"{service}: rescanned {item.title} ({state})")
            if progress:
                progress(f"      {service}: rescanned {item.title} ({state})")
        except ArrUnavailable as exc:
            result.errors.append(f"{service}: {exc}")
            if progress:
                progress(f"      {service}: could not rescan -- {exc}")
        except Exception as exc:  # noqa: BLE001 - see the docstring
            result.errors.append(f"{service}: unexpected {exc!r}")
    return result


# ------------------------------------------------------------------ renaming


def rename_preview(client: ArrWriter, service: str, item: Item) -> list[RenameProposal]:
    """What the *arr would rename inside one item, computed by the *arr itself.

    This is a real preview rather than our guess at their naming format, which
    matters because that format has half a dozen conditional tokens in it.
    """
    spec = SERVICE_COMMANDS[service]
    previous, client.timeout = client.timeout, RENAME_PREVIEW_TIMEOUT_S
    try:
        rows = client._get("rename", **{spec["id_field"]: item.item_id}) or []
    finally:
        client.timeout = previous
    out = []
    for row in rows:
        file_id = row.get("episodeFileId") or row.get("movieFileId")
        if file_id is None:
            continue
        out.append(RenameProposal(
            service=service, item=item, file_id=file_id,
            existing_path=row.get("existingPath") or "",
            new_path=row.get("newPath") or "",
        ))
    return out


def rename_apply(
    client: ArrWriter, service: str, item: Item, file_ids: list[int]
) -> str:
    spec = SERVICE_COMMANDS[service]
    command_id = _run_command(client, {
        "name": spec["rename"],
        spec["id_field"]: item.item_id,
        "files": file_ids,
    })
    if command_id is None:
        return "accepted (no command id returned)"
    return _await_command(client, command_id, RESCAN_TIMEOUT_S)


def items_we_have_touched(db, config, service: str, client: ArrWriter) -> list[Item]:
    """The series and movies holding files this project has replaced.

    Taken from `outcome` rather than from the whole library, so a rename run
    only ever proposes changes to files we are responsible for. Someone else's
    badly-named library is not ours to tidy.
    """
    settings = getattr(config, service)
    rows = db.query("SELECT DISTINCT file_path FROM outcome ORDER BY file_path")
    found: dict[int, Item] = {}
    for row in rows:
        item = locate(client, service, row["file_path"], settings.path_map)
        if item is not None:
            found.setdefault(item.item_id, item)
    return list(found.values())
