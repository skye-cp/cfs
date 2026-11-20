#!/usr/bin/env python3
"""A small Drive model standing in for rclone in cfs_drive restore/trash tests.

Models what those subcommands rely on: object IDs, same-name duplicates, a
trashed flag that `backend moveid` keeps, a recursive `backend untrash` that
ignores filters, `rmdir` that trashes (or with --drive-use-trash=false deletes
the folder and everything under it, trashed children included, as Drive does).
State lives in $MOCK_STATE (JSON: {"n": int, "objs": {id: {name, parent,
trashed, isdir, body}}}); each argv is appended to $MOCK_LOG. Anything else
exits 9. Based on core-eng-ver-4's round-2 codetest model.
"""

import hashlib
import json
import os
import sys

STATE = os.environ["MOCK_STATE"]
with open(STATE) as f:
    st = json.load(f)
objs = st["objs"]


def save() -> None:
    with open(STATE, "w") as f:
        json.dump(st, f)


def newid() -> str:
    st["n"] += 1
    return f"ID{st['n']:03d}"


def kids(pid: str, trashed: bool | None = None) -> list:
    return [
        (i, o)
        for i, o in objs.items()
        if o["parent"] == pid and (trashed is None or o["trashed"] == trashed)
    ]


def resolve(path: str, create: bool = False) -> str | None:
    pid = "root"
    for part in [p for p in path.split("/") if p]:
        m = [i for i, o in kids(pid, False) if o["name"] == part and o["isdir"]]
        if not m:
            if not create:
                return None
            i = newid()
            objs[i] = dict(name=part, parent=pid, trashed=False, isdir=True, body="")
            m = [i]
        pid = m[0]
    return pid


def rp(arg: str) -> str:
    return arg.split(":", 1)[1]


def row(i: str, o: dict) -> dict:
    b = o["body"].encode()
    r = {
        "Path": o["name"],
        "Name": o["name"],
        "Size": -1 if o["isdir"] else len(b),
        "ModTime": "2026-11-20T16:00:00.000Z",
        "IsDir": o["isdir"],
        "ID": i,
    }
    if not o["isdir"]:
        r["Hashes"] = {"md5": hashlib.md5(b).hexdigest()}  # noqa: S324
    return r


def target() -> str:
    """The remote path argument: the first non-flag argument with a colon."""
    return next(rp(x) for x in a[1:] if ":" in x and not x.startswith("-"))


def fail(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def purge(pid: str) -> None:
    for i, _ in kids(pid):
        purge(i)
    del objs[pid]


a = sys.argv[1:]
with open(os.environ.get("MOCK_LOG", os.devnull), "a") as f:
    f.write(json.dumps(a) + "\n")
cmd = a[0] if a else ""
if cmd == "listremotes":
    print("gdrive: drive")
    sys.exit(0)
if cmd == "lsjson":
    d = resolve(target())
    if d is None:
        fail("directory not found", 3)
    print(json.dumps([row(i, o) for i, o in kids(d, "--drive-trashed-only" in a)]))
    sys.exit(0)
if cmd == "mkdir":
    resolve(target(), create=True)
    save()
    sys.exit(0)
if cmd == "rmdir":
    d = resolve(target())
    if d is None or kids(d, False):
        fail("rmdir failed: missing or not empty")
    if "--drive-use-trash=false" in a:
        purge(d)  # permanent: takes trashed children with it
    else:
        objs[d]["trashed"] = True
    save()
    sys.exit(0)
if cmd == "deletefile":
    parent, _, name = target().rpartition("/")
    d = resolve(parent)
    m = [i for i, o in kids(d, False) if o["name"] == name and not o["isdir"]]
    if len(m) != 1:
        fail("deletefile: not exactly one")
    objs[m[0]]["trashed"] = True
    save()
    sys.exit(0)
if cmd == "backend" and a[1] == "moveid":
    i = a[3]
    parent, _, name = rp(a[4]).rpartition("/")
    d = resolve(parent)
    if i not in objs or d is None:
        fail("moveid failed")
    objs[i]["parent"], objs[i]["name"] = d, name  # trashed flag kept, like Drive
    save()
    sys.exit(0)
if cmd == "backend" and a[1] == "untrash":
    n = 0

    def walk(pid: str) -> None:
        global n
        for i, o in kids(pid):
            if o["trashed"]:
                o["trashed"] = False
                n += 1
            if o["isdir"]:
                walk(i)

    walk(resolve(rp(a[2])))
    save()
    print(json.dumps({"Untrashed": n, "Errors": 0}))
    sys.exit(0)
fail("mock: unsupported " + " ".join(a), 9)
