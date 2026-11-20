#!/usr/bin/env python3
"""Fault-injecting wrapper around the real rclone, for cfs_drive tests.

Environment knobs (all optional):
  SHIM_DUP_NAME=<name>      lsjson rows with this Name are listed twice (second ID 'dup-<n>')
  SHIM_COPYTO=fail          copyto exits 1 without writing
  SHIM_COPYTO=truncate:<n>  copyto uploads only the first <n> bytes, exits 0
  SHIM_COPYTO=failafter     copyto writes the full file, then exits 1
  SHIM_COPYTO_ONCE=1        apply SHIM_COPYTO only to the first copyto (rollback works)
  SHIM_DUP_AFTER_COPYTO=1   apply SHIM_DUP_NAME only once a copyto has run (retried-upload twin)
  SHIM_LOG=<file>           append each argv as one JSON line
  SHIM_HOOK_CMD=<cmd>       once, around the first <cmd> (copyto, cat, ...), act as
                            another writer: write SHIM_HOOK_DATA to the local path
                            SHIM_HOOK_PATH (SHIM_HOOK_APPEND=1 appends instead);
                            SHIM_HOOK_WHEN=before|after (default after);
                            SHIM_HOOK_DONE=<file> marks that it fired
"""

import json
import os
import subprocess  # noqa: S404
import sys
import tempfile

REAL = os.environ.get("SHIM_REAL_RCLONE", "rclone")
args = sys.argv[1:]
if os.environ.get("SHIM_LOG"):
    with open(os.environ["SHIM_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")


def hook(when: str) -> None:
    cmd = os.environ.get("SHIM_HOOK_CMD")
    done = os.environ.get("SHIM_HOOK_DONE")
    if not cmd or not done or not args or args[0] != cmd:
        return
    if os.environ.get("SHIM_HOOK_WHEN", "after") != when or os.path.exists(done):
        return
    data = os.environ["SHIM_HOOK_DATA"].encode()
    with open(
        os.environ["SHIM_HOOK_PATH"],
        "ab" if os.environ.get("SHIM_HOOK_APPEND") else "wb",
    ) as f:
        f.write(data)
    open(done, "w").close()


hook("before")
mode = os.environ.get("SHIM_COPYTO", "")
if args and args[0] == "copyto" and mode:
    flag = os.environ.get("SHIM_COPYTO_ONCE") and os.environ.get("SHIM_LOG")
    first = True
    if flag:
        with open(os.environ["SHIM_LOG"]) as f:
            first = sum(1 for line in f if json.loads(line)[:1] == ["copyto"]) == 1
    if first:
        if mode == "fail":
            sys.stderr.write("shim: injected copyto failure\n")
            sys.exit(1)
        if mode.startswith("truncate:"):
            n = int(mode.split(":", 1)[1])
            data = open(args[1], "rb").read()[:n]
            with tempfile.NamedTemporaryFile(delete=False) as t:
                t.write(data)
            st = os.stat(args[1])
            os.utime(t.name, (st.st_atime, st.st_mtime))  # rclone keeps our modtime
            cmd = [REAL, "copyto", t.name, args[2]]
            rc = subprocess.run(cmd, check=False).returncode  # noqa: S603
            os.unlink(t.name)
            sys.exit(rc)
        if mode == "failafter":
            subprocess.run([REAL, *args], check=False)  # noqa: S603
            sys.exit(1)

proc = subprocess.run([REAL, *args], check=False, capture_output=True)  # noqa: S603
hook("after")
out = proc.stdout
dup = os.environ.get("SHIM_DUP_NAME")
if dup and os.environ.get("SHIM_DUP_AFTER_COPYTO") and os.environ.get("SHIM_LOG"):
    with open(os.environ["SHIM_LOG"]) as f:
        if not any(json.loads(line)[:1] == ["copyto"] for line in f):
            dup = None
if args and args[0] == "lsjson" and dup and proc.returncode == 0:
    rows = json.loads(out or b"[]")
    extra = []
    for i, r in enumerate(rows):
        if r.get("Name") == dup:
            c = dict(r)
            c["ID"] = f"dup-{i}"
            extra.append(c)
    out = json.dumps(rows + extra).encode()
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(proc.stderr)
sys.exit(proc.returncode)
