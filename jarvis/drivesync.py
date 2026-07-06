"""Two-way sync between the Google Drive 'raw' folder (mounted by Google Drive for Desktop
at G:\\My Drive\\raw) and the vault's raw inbox. Either side is editable; changes propagate
both ways:

  * add/edit a file on either side  -> copied to the other (newer wins on a conflict)
  * delete a file on either side     -> deleted from the other

A manifest (drivesync_state.json) of last-synced files distinguishes a NEW file from a
DELETED one. Hard guard: if either folder is missing/unmounted, NOTHING is copied or
deleted that poll (an offline Drive or a vanished raw folder must never mass-delete the
other side).

Note: Jarvis writes its own notes into raw, so those now upload to Drive too — that is
inherent to two-way and intended per the request."""
import json
import logging
import shutil
import threading
import time
from pathlib import Path

from jarvis import config
from jarvis.audit import audit

log = logging.getLogger("jarvis.drivesync")

DEFAULT_SRC = r"G:\My Drive\raw"
STATE = config.REPO_ROOT / "drivesync_state.json"  # gitignored; list of last-synced relpaths


def start(cfg):
    """Spawn the sync as a daemon thread (same pattern as voice.start). Never raises."""
    if not cfg.get("gdrive_sync_enabled", True):
        return
    threading.Thread(target=_run, args=(cfg,), name="jarvis-drivesync", daemon=True).start()


def _run(cfg):
    a = Path(cfg.get("gdrive_raw_path") or DEFAULT_SRC)  # Google Drive side
    b = Path(cfg["vault_path"]) / "raw"  # vault side
    interval = cfg.get("gdrive_sync_interval", 5)
    log.info("Drive two-way sync: %s <-> %s every %ss", a, b, interval)
    while True:
        try:
            down, up, deleted = sync_once(a, b)
            if down or up or deleted:
                log.info("Drive raw: %d down, %d up, %d deleted", down, up, deleted)
        except Exception as exc:
            log.warning("Drive sync error: %s", exc)
        time.sleep(interval)


def _load_state(path):
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):  # missing or empty file -> nothing tracked yet
        return set()


def _save_state(path, synced):
    try:
        Path(path).write_text(json.dumps(sorted(synced)), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save drivesync state: %s", exc)


def _files(root):
    """{relpath: mtime} for every file under root (recursive)."""
    out = {}
    for f in Path(root).rglob("*"):
        if f.is_file():
            try:
                out[f.relative_to(root).as_posix()] = f.stat().st_mtime
            except OSError:
                pass
    return out


def _copy(src_file, dst_file):
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dst_file)  # preserves mtime, so the reverse poll sees no change


def sync_once(a, b, state_path=None):
    """One two-way reconcile of a <-> b. Returns (a_to_b, b_to_a, deleted).

    # ponytail: last-write-wins on a both-sides edit (mtime, 1s slop); no 3-way merge —
    # add one only if simultaneous edits of the same file become a real problem.
    """
    a, b = Path(a), Path(b)
    state_path = Path(state_path) if state_path else STATE
    known = _load_state(state_path)
    if not a.exists() or not b.exists():
        return 0, 0, 0  # a side is missing/unmounted -> do nothing (never mass-delete)
    A, B = _files(a), _files(b)
    a2b = b2a = deleted = 0
    for rel in set(A) | set(B) | known:
        inA, inB = rel in A, rel in B
        try:
            if inA and inB:  # both present -> push the newer over the older
                if A[rel] > B[rel] + 1:
                    _copy(a / rel, b / rel); audit("gdrive_pull", file=rel); a2b += 1
                elif B[rel] > A[rel] + 1:
                    _copy(b / rel, a / rel); audit("gdrive_push", file=rel); b2a += 1
            elif inA and not inB:
                if rel in known:  # existed before, gone from b -> deleted in b -> remove from Drive
                    (a / rel).unlink(); audit("gdrive_delete", file=rel, side="drive"); deleted += 1
                else:  # new on Drive -> copy down
                    _copy(a / rel, b / rel); audit("gdrive_pull", file=rel); a2b += 1
            elif inB and not inA:
                if rel in known:  # deleted on Drive -> remove from vault
                    (b / rel).unlink(); audit("gdrive_delete", file=rel, side="vault"); deleted += 1
                else:  # new in vault -> copy up to Drive
                    _copy(b / rel, a / rel); audit("gdrive_push", file=rel); b2a += 1
        except OSError as exc:  # locked / mid-download / permission -> retry next poll
            log.warning("skip %s (%s)", rel, exc)
    _save_state(state_path, set(_files(a)) & set(_files(b)))  # reconciled = present on both sides
    return a2b, b2a, deleted


if __name__ == "__main__":  # ponytail: runnable check
    import tempfile

    with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db, tempfile.TemporaryDirectory() as st:
        A, B, state = Path(da), Path(db), Path(st) / "state.json"
        (A / "fromdrive.txt").write_text("drive")
        (B / "fromvault.txt").write_text("vault")
        assert sync_once(A, B, state) == (1, 1, 0)  # each new file crosses over
        assert (B / "fromdrive.txt").read_text() == "drive" and (A / "fromvault.txt").read_text() == "vault"
        assert sync_once(A, B, state) == (0, 0, 0)  # stable, no loop
        (A / "fromdrive.txt").unlink()  # delete on Drive side
        assert sync_once(A, B, state) == (0, 0, 1)
        assert not (B / "fromdrive.txt").exists(), "Drive delete -> vault delete"
        (B / "fromvault.txt").unlink()  # delete on vault side
        assert sync_once(A, B, state) == (0, 0, 1)
        assert not (A / "fromvault.txt").exists(), "vault delete -> Drive delete"
        # edit on vault side wins upward
        (A / "shared.txt").write_text("v1"); sync_once(A, B, state)
        time.sleep(1.1); (B / "shared.txt").write_text("v2-newer")
        assert sync_once(A, B, state)[1] == 1 and (A / "shared.txt").read_text() == "v2-newer"
        # unmounted Drive -> delete NOTHING
        assert sync_once(Path(da) / "gone", B, state) == (0, 0, 0)
        assert (B / "shared.txt").exists()
        print("drivesync two-way self-check passed")
