#!/usr/bin/env python3
"""Guarded rclone wrapper for Drive-backed CFS reads and writes.

Every read-modify-write of an existing CFS file on Google Drive goes through
the Drive API (the rclone CLI), never through a FUSE mount such as ~/gdrive:
a mount can serve a stale listing or a body cut to a stale size.

Subcommands (all print one JSON line on success, exit 2 with REFUSED: on
stderr when a guard fails, and never touch the remote after a guard fails):

  read REMOTE LOCAL [--state STATE]   rclone cat into LOCAL; STATE records
                                      id/size/md5/modtime for a later write
  write LOCAL REMOTE (--base STATE | --expect-md5 MD5 | --create)
                                      validate, compare-and-swap, copyto,
                                      verify, roll back on a bad upload
  moveto SRC DST                      rename one object; refuse if DST exists
  trash REMOTE [--id ID]              move one object to Drive trash and
                                      confirm it in the trash listing
  lsjson REMOTE_DIR                   authoritative listing; refuse on
                                      same-name duplicates

Never reads credentials: rclone owns authentication. Set CFS_DRIVE_RCLONE to
use another rclone binary (the tests do).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import subprocess  # noqa: S404 - rclone is the API client; argv lists only
import sys
import time
from pathlib import Path

RCLONE = os.environ.get("CFS_DRIVE_RCLONE", "rclone")
NOT_FOUND = ("directory not found", "object not found", "file not found")
TEXT_SUFFIXES = (".md", ".log", ".yaml", ".yml", ".txt", ".json", ".csv")


class Refused(Exception):
    """A guard failed; the message says which and what state was left."""


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [RCLONE, *args], check=False, capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise Refused(
            f"rclone {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}"
        )
    return proc


def split(remote: str) -> tuple[str, str]:
    """'gdrive:a/b/c.md' -> ('gdrive:a/b', 'c.md')."""
    if ":" not in remote:
        raise Refused(f"{remote} is not a remote path (expected <remote>:<path>)")
    head, _, path = remote.partition(":")
    path = path.rstrip("/")
    parent, name = posixpath.split(path)
    if not name:
        raise Refused(f"{remote} has no file name")
    return f"{head}:{parent}", name


def list_dir(remote_dir: str, trashed: bool = False) -> list[dict]:
    """All objects directly in a directory, duplicates included. Missing dir -> []."""
    args = ["lsjson", remote_dir, "--hash", "--no-mimetype"]
    if trashed:
        args.append("--drive-trashed-only")
    proc = run(args, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        if any(s in err for s in NOT_FOUND):
            return []
        raise Refused(f"rclone lsjson {remote_dir} failed: {err.strip()[-500:]}")
    return json.loads(proc.stdout or "[]")


def objects(remote: str, trashed: bool = False) -> list[dict]:
    """Every object (file or folder) named exactly like REMOTE in its parent."""
    parent, name = split(remote)
    return [r for r in list_dir(parent, trashed) if r.get("Name") == name]


def one_file(rows: list[dict], remote: str) -> dict | None:
    if not rows:
        return None
    if len(rows) > 1:
        ids = [r.get("ID") for r in rows]
        raise Refused(
            f"{remote} has {len(rows)} same-name objects {ids}; resolve the duplicate first"
        )
    if rows[0].get("IsDir"):
        raise Refused(f"{remote} is a folder")
    return rows[0]


def md5_of(row: dict) -> str | None:
    return (row.get("Hashes") or {}).get("md5")


def state_of(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.get("ID"),
        "size": row["Size"],
        "md5": md5_of(row),
        "modtime": row.get("ModTime"),
    }


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 - Drive's content checksum


def emit(obj: dict) -> int:
    print(json.dumps({"ok": True, **obj}))
    return 0


def validate_body(local: str, data: bytes, binary: bool) -> None:
    """All content checks run before anything is uploaded."""
    if not data:
        raise Refused(f"{local} is empty")
    if binary:
        return
    if not local.endswith(TEXT_SUFFIXES):
        raise Refused(
            f"{local}: not a known text type; pass --binary to skip text checks"
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Refused(f"{local} is not valid UTF-8 ({e})") from e
    if not data.endswith(b"\n"):
        raise Refused(f"{local} does not end with a newline")
    if b"\x00" in data:
        raise Refused(f"{local} contains NUL bytes")


def cat(remote: str) -> bytes:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [RCLONE, "cat", remote], check=False, capture_output=True
    )
    if proc.returncode != 0:
        raise Refused(
            f"rclone cat {remote} failed: {proc.stderr.decode(errors='replace').strip()[-500:]}"
        )
    return proc.stdout


def cmd_read(remote: str, local: str, state_path: str | None) -> int:
    before = one_file(objects(remote), remote)
    if before is None:
        raise Refused(f"{remote} does not exist")
    data = cat(remote)
    after = one_file(objects(remote), remote)
    st = state_of(before)
    if state_of(after) != st:
        raise Refused(
            f"{remote} changed while reading ({st} -> {state_of(after)}); read again"
        )
    if len(data) != st["size"] or (st["md5"] and md5_bytes(data) != st["md5"]):
        raise Refused(
            f"{remote}: downloaded {len(data)} B md5 {md5_bytes(data)} != listed {st['size']} B {st['md5']}"
        )
    Path(local).write_bytes(data)
    st = {"remote": remote, **st}
    if state_path:
        Path(state_path).write_text(json.dumps(st, indent=2) + "\n")
    return emit({"read": remote, "local": local, **st})


def cmd_write(
    local: str,
    remote: str,
    base: dict | None,
    create: bool,
    binary: bool,
    allow_shrink: bool,
    preimage_dir: str | None,
) -> int:
    data = Path(local).read_bytes()
    validate_body(local, data, binary)
    want = {"size": len(data), "md5": md5_bytes(data)}

    cur = one_file(objects(remote), remote)
    if create:
        if cur is not None:
            raise Refused(
                f"--create but {remote} already exists (id {cur.get('ID')}); read it and pass --base"
            )
    else:
        if base is None:
            raise Refused("pass --base STATE (from `read`), --expect-md5, or --create")
        if cur is None:
            raise Refused(f"{remote} does not exist any more (expected {base})")
        now = state_of(cur)
        for k in ("id", "md5", "size", "modtime"):
            if base.get(k) is not None and base[k] != now[k]:
                raise Refused(
                    f"{remote} changed since it was read: {k} {base[k]} -> {now[k]}; re-read and redo the edit"
                )
        if not now["md5"]:
            raise Refused(f"{remote} has no md5 (Google-native file?); not supported")
        if want["size"] < now["size"] and not allow_shrink:
            raise Refused(
                f"{local} ({want['size']} B) is smaller than {remote} ({now['size']} B); "
                "a shrinking edit needs --allow-shrink (truncation guard)"
            )
        if want["md5"] == now["md5"]:
            return emit({"remote": remote, "id": now["id"], "unchanged": True, **want})

    pre_path = None
    pre_md5 = None
    if cur is not None:
        pre = cat(remote)
        pre_md5 = md5_bytes(pre)
        if pre_md5 != md5_of(cur):
            raise Refused(
                f"{remote}: pre-image md5 {pre_md5} != listed {md5_of(cur)}; nothing written"
            )
        if preimage_dir:
            d = Path(preimage_dir)
            d.mkdir(parents=True, exist_ok=True)
            pre_path = (
                d
                / f"{time.strftime('%Y%m%dT%H%M%S')}__{cur.get('ID') or 'noid'}__{split(remote)[1]}"
            )
            pre_path.write_bytes(pre)
        # last check, immediately before the upload
        if state_of(one_file(objects(remote), remote)) != state_of(cur):
            raise Refused(
                f"{remote} changed during the write preparation; nothing written"
            )

    up = run(["copyto", local, remote], check=False)
    after = objects(remote)
    problem = None
    if up.returncode != 0:
        problem = f"copyto failed (exit {up.returncode}): {up.stderr.strip()[-300:]}"
    elif len(after) != 1:
        problem = f"{len(after)} objects after copyto: {[r.get('ID') for r in after]}"
    else:
        got = state_of(after[0])
        if got["size"] != want["size"] or got["md5"] != want["md5"]:
            problem = f"uploaded {got['size']} B {got['md5']} != intended {want['size']} B {want['md5']}"
        elif cur is not None and got["id"] != cur.get("ID"):
            problem = f"id changed {cur.get('ID')} -> {got['id']}"
    if problem is None:
        return emit(
            {
                "remote": remote,
                "id": after[0].get("ID"),
                "preimage": str(pre_path) if pre_path else None,
                "preimage_md5": pre_md5,
                **want,
            }
        )

    # Roll back to the pre-image when there was one and the name is still single.
    note = "no pre-image (new file); check the remote by hand"
    if cur is not None and len(after) == 1 and md5_of(after[0]) == pre_md5:
        note = "remote still holds the pre-image; nothing to roll back"
    elif cur is not None and pre_path is not None and len(after) <= 1:
        rb = run(["copyto", str(pre_path), remote], check=False)
        chk = objects(remote)
        ok = rb.returncode == 0 and len(chk) == 1 and md5_of(chk[0]) == pre_md5
        note = f"rolled back to pre-image {pre_path}: {'verified' if ok else 'FAILED, fix by hand'}"
    elif cur is not None:
        note = f"NOT rolled back (no --preimage-dir or duplicates); pre-image md5 {pre_md5}"
    raise Refused(f"{remote}: {problem}; {note}")


def cmd_moveto(src: str, dst: str) -> int:
    before = one_file(objects(src), src)
    if before is None:
        raise Refused(f"{src} does not exist")
    existing = objects(dst)
    if existing:
        raise Refused(
            f"{dst} already exists ({[r.get('ID') for r in existing]}); merge or trash it first"
        )
    st = state_of(before)
    run(["moveto", src, dst])
    left = objects(src)
    if left:
        raise Refused(
            f"{src} still has {len(left)} object(s) after moveto: {[x.get('ID') for x in left]}"
        )
    after = one_file(objects(dst), dst)
    if after is None:
        raise Refused(f"{dst} missing after moveto (source id {st['id']})")
    got = state_of(after)
    if got["id"] != st["id"] or got["size"] != st["size"] or got["md5"] != st["md5"]:
        raise Refused(f"{dst} is {got}, expected {st}")
    return emit({"src": src, "dst": dst, **st})


def remote_type(remote: str) -> str:
    name = remote.partition(":")[0]
    if name == "" or name.startswith(":"):
        return "local" if remote.startswith(":local") else "unknown"
    for line in run(["listremotes", "--long"]).stdout.splitlines():
        r, _, t = line.partition(":")
        if r.strip() == name:
            return t.strip()
    raise Refused(f"no rclone remote named {name}")


def cmd_trash(remote: str, want_id: str | None) -> int:
    rtype = remote_type(remote)
    on_drive = rtype == "drive"
    if not on_drive and os.environ.get("CFS_DRIVE_ALLOW_NON_DRIVE") != "1":
        raise Refused(
            f"{remote} is on a {rtype} remote; trash is Drive-only (no trash elsewhere)"
        )
    rows = objects(remote)
    if not rows:
        raise Refused(f"{remote} does not exist")
    if any(r.get("IsDir") for r in rows):
        raise Refused(f"{remote} is a folder; not trashing folders")
    ids = [r.get("ID") for r in rows]
    if want_id is not None and want_id not in ids:
        raise Refused(f"id {want_id} is not an object at {remote} (found {ids})")
    if len(rows) > 1 and want_id is None:
        raise Refused(
            f"{remote} has {len(rows)} same-name objects {ids}; pass --id to pick one"
        )
    target = next(r for r in rows if want_id is None or r.get("ID") == want_id)
    st = state_of(target)
    parent, name = split(remote)
    path = remote
    if len(rows) > 1:
        # Give the chosen object a unique name by ID (rclone backend moveid), then trash that path.
        path = f"{parent}/{name}.cfs-trash-{st['id']}"
        if objects(path):
            raise Refused(f"{path} already exists")
        run(["backend", "moveid", remote.partition(":")[0] + ":", st["id"], path])
        moved = one_file(objects(path), path)
        if moved is None or moved.get("ID") != st["id"]:
            raise Refused(
                f"moveid of {st['id']} to {path} not confirmed; nothing trashed"
            )
    run(["deletefile", "--drive-use-trash", path] if on_drive else ["deletefile", path])
    remaining = [r.get("ID") for r in objects(path)] + [
        r.get("ID") for r in objects(remote)
    ]
    if st["id"] is not None and st["id"] in remaining:
        raise Refused(f"{st['id']} still listed after deletefile")
    if len(rows) > 1 and len(objects(remote)) != len(rows) - 1:
        raise Refused(f"{remote}: expected {len(rows) - 1} object(s) left")
    in_trash = None
    if on_drive:
        tr = [r for r in list_dir(parent, trashed=True) if r.get("ID") == st["id"]]
        in_trash = bool(tr)
        if not in_trash:
            raise Refused(
                f"{st['id']} gone from {parent} but not in its trash listing; check Drive trash"
            )
    return emit({"trashed": remote, "trashed_as": path, "in_trash": in_trash, **st})


def cmd_lsjson(remote_dir: str) -> int:
    rows = list_dir(remote_dir)
    print(json.dumps(rows, indent=2))
    names = [r["Name"] for r in rows]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise Refused(f"duplicate names: {dupes}")
    return 0


def load_base(args: argparse.Namespace) -> dict | None:
    if args.base:
        return json.loads(Path(args.base).read_text())
    if args.expect_md5:
        return {"md5": args.expect_md5}
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read", help="rclone cat REMOTE to LOCAL and record its state")
    r.add_argument("remote")
    r.add_argument("local")
    r.add_argument(
        "--state", help="write id/size/md5/modtime JSON here, for write --base"
    )
    w = sub.add_parser("write", help="guarded rclone copyto LOCAL REMOTE")
    w.add_argument("local")
    w.add_argument("remote")
    g = w.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--base", help="state JSON from `read`: refuse if REMOTE changed since"
    )
    g.add_argument(
        "--expect-md5", help="refuse unless REMOTE's current md5 equals this"
    )
    g.add_argument("--create", action="store_true", help="REMOTE must not exist yet")
    w.add_argument(
        "--allow-shrink", action="store_true", help="allow the new body to be smaller"
    )
    w.add_argument(
        "--binary",
        action="store_true",
        help="skip text checks (UTF-8, trailing newline)",
    )
    w.add_argument(
        "--preimage-dir", help="save the replaced body here (enables rollback)"
    )
    m = sub.add_parser("moveto", help="rclone moveto SRC DST; refuse if DST exists")
    m.add_argument("src")
    m.add_argument("dst")
    t = sub.add_parser("trash", help="move one object to Drive trash and confirm it")
    t.add_argument("remote")
    t.add_argument(
        "--id", help="Drive ID to trash; required when the name is duplicated"
    )
    ls = sub.add_parser(
        "lsjson", help="list a folder via the API; refuse on duplicate names"
    )
    ls.add_argument("remote_dir")
    a = p.parse_args(argv)
    try:
        if a.cmd == "read":
            return cmd_read(a.remote, a.local, a.state)
        if a.cmd == "write":
            return cmd_write(
                a.local,
                a.remote,
                load_base(a),
                a.create,
                a.binary,
                a.allow_shrink,
                a.preimage_dir,
            )
        if a.cmd == "moveto":
            return cmd_moveto(a.src, a.dst)
        if a.cmd == "trash":
            return cmd_trash(a.remote, a.id)
        if a.cmd == "lsjson":
            return cmd_lsjson(a.remote_dir)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
