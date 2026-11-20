#!/usr/bin/env python3
"""Thin rclone wrapper for Drive-backed CFS writes.

Refuses to complete when the post-write size or MD5 does not match.
Never writes through a FUSE mount.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def lsjson(remote: str) -> list[dict]:
    out = run(["rclone", "lsjson", remote, "--hash"]).stdout
    return json.loads(out or "[]")


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def reject(msg: str) -> int:
    print(f"REFUSED: {msg}", file=sys.stderr)
    return 2


def cmd_write(local: str, remote: str) -> int:
    data = Path(local).read_bytes()
    expected = {"size": len(data), "md5": md5_bytes(data)}
    run(["rclone", "copyto", local, remote])
    meta = lsjson(remote)
    if len(meta) != 1:
        return reject(f"{remote} has {len(meta)} objects after copyto; expected 1")
    got_size = meta[0]["Size"]
    got_md5 = (meta[0].get("Hashes") or {}).get("md5")
    if got_size != expected["size"] or got_md5 != expected["md5"]:
        return reject(
            f"{remote} size/md5 {got_size}/{got_md5} != intended "
            f"{expected['size']}/{expected['md5']} (id={meta[0].get('ID')})"
        )
    if not data.endswith(b"\n"):
        return reject(f"{local} does not end with a newline")
    print(json.dumps({"ok": True, "remote": remote, "id": meta[0].get("ID"), **expected}))
    return 0


def cmd_moveto(src: str, dst: str) -> int:
    before = lsjson(src)
    if len(before) != 1:
        return reject(f"{src} has {len(before)} objects before moveto; expected 1")
    fid = before[0].get("ID")
    exp_size = before[0]["Size"]
    exp_md5 = (before[0].get("Hashes") or {}).get("md5")
    run(["rclone", "moveto", src, dst])
    left = lsjson(src)
    if left:
        return reject(f"{src} still has {len(left)} object(s) after moveto: {[x.get('ID') for x in left]}")
    after = lsjson(dst)
    if len(after) != 1:
        return reject(f"{dst} has {len(after)} objects after moveto; expected 1")
    if after[0].get("ID") != fid:
        return reject(f"{dst} id {after[0].get('ID')} != source id {fid}")
    got_md5 = (after[0].get("Hashes") or {}).get("md5")
    if after[0]["Size"] != exp_size or got_md5 != exp_md5:
        return reject(f"{dst} size/md5 changed during moveto")
    print(json.dumps({"ok": True, "src": src, "dst": dst, "id": fid, "size": exp_size, "md5": exp_md5}))
    return 0


def cmd_lsf(remote: str) -> int:
    rows = lsjson(remote if not remote.endswith("/") else remote)
    # If remote is a directory, rclone lsjson lists children when path is the dir.
    print(json.dumps(rows, indent=2))
    names = [r["Name"] for r in rows if not r.get("IsDir")]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        return reject(f"duplicate names: {dupes}")
    return 0


def cmd_trash_id(file_id: str) -> int:
    """Trash a Drive object by ID (needed when a folder has two files of the same name)."""
    import urllib.request

    cfg = json.loads(run(["rclone", "config", "dump"]).stdout)
    # Prefer the gdrive remote; fall back to the first remote that has a token.
    token = None
    for name, ent in cfg.items():
        if isinstance(ent, dict) and ent.get("token"):
            if name == "gdrive" or token is None:
                token = json.loads(ent["token"])["access_token"]
    if not token:
        return reject("no rclone remote token found")
    body = json.dumps({"trashed": True}).encode()
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        got = json.loads(resp.read().decode())
    print(json.dumps({"ok": True, "id": file_id, "trashed": True, "name": got.get("name")}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="rclone copyto LOCAL REMOTE and verify size+md5")
    w.add_argument("local")
    w.add_argument("remote")
    m = sub.add_parser("moveto", help="rclone moveto SRC DST and verify one ID")
    m.add_argument("src")
    m.add_argument("dst")
    l = sub.add_parser("lsjson", help="rclone lsjson --hash; refuse if names are duplicated")
    l.add_argument("remote")
    t = sub.add_parser("trash-id", help="PATCH Drive file trashed=true by ID")
    t.add_argument("file_id")
    args = p.parse_args()
    try:
        if args.cmd == "write":
            return cmd_write(args.local, args.remote)
        if args.cmd == "moveto":
            return cmd_moveto(args.src, args.dst)
        if args.cmd == "lsjson":
            return cmd_lsf(args.remote)
        if args.cmd == "trash-id":
            return cmd_trash_id(args.file_id)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(e.stderr or str(e))
        return reject(f"rclone failed: {e.cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
