"""Tests for tools/rclone/cfs_drive.py against rclone's local backend.

Run: python3 -m unittest discover -s tools/rclone/tests -v
Needs the rclone binary on PATH. Faults (duplicates, failed or short uploads)
are injected by rclone_shim.py, which wraps the real rclone.
"""

import hashlib
import json
import os
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "cfs_drive.py"
SHIM = HERE / "rclone_shim.py"


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()  # noqa: S324


@unittest.skipUnless(shutil.which("rclone"), "rclone not installed")
class CfsDriveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cfs_drive_test_"))
        self.root = self.tmp / "remote" / "todo"
        self.root.mkdir(parents=True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        cfg = self.tmp / "rclone.conf"
        cfg.write_text("[t]\ntype = local\n")
        self.log = self.tmp / "shim.log"
        self.env = {
            **os.environ,
            "RCLONE_CONFIG": str(cfg),
            "CFS_DRIVE_RCLONE": str(SHIM),
            "SHIM_REAL_RCLONE": shutil.which("rclone"),
            "SHIM_LOG": str(self.log),
        }
        for k in (
            "SHIM_DUP_NAME",
            "SHIM_DUP_AFTER_COPYTO",
            "SHIM_COPYTO",
            "SHIM_COPYTO_ONCE",
            "CFS_DRIVE_ALLOW_NON_DRIVE",
            "SHIM_HOOK_CMD",
            "SHIM_HOOK_WHEN",
            "SHIM_HOOK_PATH",
            "SHIM_HOOK_DATA",
            "SHIM_HOOK_APPEND",
            "SHIM_HOOK_DONE",
        ):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    # helpers
    def r(self, name: str) -> str:
        return f"t:{self.root}/{name}"

    def put(self, name: str, data: bytes) -> Path:
        p = self.root / name
        p.write_bytes(data)
        return p

    def local(self, name: str, data: bytes) -> str:
        p = self.work / name
        p.write_bytes(data)
        return str(p)

    def tool(self, *args, **env):
        e = {**self.env, **env}
        p = subprocess.run(  # noqa: S603
            [sys.executable, str(TOOL), *args],
            env=e,
            capture_output=True,
            text=True,
            check=False,
        )
        return p.returncode, p.stdout, p.stderr

    def copytos(self) -> int:
        if not self.log.exists():
            return 0
        return sum(
            1
            for line in self.log.read_text().splitlines()
            if json.loads(line)[:1] == ["copyto"]
        )

    def read_state(self, name: str) -> str:
        st = str(self.work / f"{name}.state.json")
        rc, _, err = self.tool(
            "read", self.r(name), str(self.work / name), "--state", st
        )
        self.assertEqual(rc, 0, err)
        return st

    # write: create
    def test_create_new_file(self):
        rc, out, err = self.tool(
            "write", self.local("a.md", b"hello\n"), self.r("a.md"), "--create"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["md5"], md5(b"hello\n"))
        self.assertEqual((self.root / "a.md").read_bytes(), b"hello\n")

    def test_create_refuses_existing_name(self):
        self.put("a.md", b"OTHER\n")
        rc, _, err = self.tool(
            "write", self.local("a.md", b"new\n"), self.r("a.md"), "--create"
        )
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"OTHER\n")
        self.assertEqual(self.copytos(), 0)

    # write: validation happens before any upload (M5a)
    def test_no_trailing_newline_refused_before_upload(self):
        rc, _, err = self.tool(
            "write", self.local("a.md", b"no newline"), self.r("a.md"), "--create"
        )
        self.assertEqual(rc, 2)
        self.assertIn("newline", err)
        self.assertFalse((self.root / "a.md").exists())
        self.assertEqual(self.copytos(), 0)

    def test_invalid_utf8_refused(self):
        rc, _, err = self.tool(
            "write", self.local("a.md", b"\xff\xfe bad\n"), self.r("a.md"), "--create"
        )
        self.assertEqual(rc, 2)
        self.assertIn("UTF-8", err)
        self.assertEqual(self.copytos(), 0)

    def test_empty_refused(self):
        rc, _, err = self.tool(
            "write", self.local("a.md", b""), self.r("a.md"), "--create"
        )
        self.assertEqual(rc, 2)
        self.assertIn("empty", err)

    def test_mode_required(self):
        rc, _, err = self.tool("write", self.local("a.md", b"x\n"), self.r("a.md"))
        self.assertEqual(rc, 2)  # argparse: one of --base/--expect-md5/--create

    def test_unknown_type_needs_binary(self):
        rc, _, err = self.tool(
            "write", self.local("a.bin", b"\x00\x01"), self.r("a.bin"), "--create"
        )
        self.assertEqual(rc, 2)
        rc, _, err = self.tool(
            "write",
            self.local("a.bin", b"\x00\x01"),
            self.r("a.bin"),
            "--create",
            "--binary",
        )
        self.assertEqual(rc, 0, err)

    # write: read-modify-write with compare-and-swap (M5c, M5d)
    def test_edit_with_base_and_preimage(self):
        self.put("a.md", b"line 1\n")
        st = self.read_state("a.md")
        new = self.local("a.new.md", b"line 1\nline 2\n")
        pre = self.work / "pre"
        rc, out, err = self.tool(
            "write", new, self.r("a.md"), "--base", st, "--preimage-dir", str(pre)
        )
        self.assertEqual(rc, 0, err)
        res = json.loads(out)
        self.assertEqual((self.root / "a.md").read_bytes(), b"line 1\nline 2\n")
        self.assertEqual(Path(res["preimage"]).read_bytes(), b"line 1\n")
        self.assertEqual(res["preimage_md5"], md5(b"line 1\n"))

    def test_concurrent_edit_refused(self):
        self.put("a.md", b"line 1\n")
        st = self.read_state("a.md")
        self.put("a.md", b"line 1\nother agent\n")  # someone else writes meanwhile
        rc, _, err = self.tool(
            "write",
            self.local("a.new.md", b"line 1\nmine\n"),
            self.r("a.md"),
            "--base",
            st,
        )
        self.assertEqual(rc, 2)
        self.assertIn("changed since it was read", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"line 1\nother agent\n")
        self.assertEqual(self.copytos(), 0)

    def test_expect_md5_mismatch_refused(self):
        self.put("a.md", b"v1\n")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"v2 longer\n"),
            self.r("a.md"),
            "--expect-md5",
            md5(b"v0\n"),
        )
        self.assertEqual(rc, 2)
        self.assertEqual((self.root / "a.md").read_bytes(), b"v1\n")

    def test_expect_md5_match_writes(self):
        self.put("a.md", b"v1\n")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"v2 longer\n"),
            self.r("a.md"),
            "--expect-md5",
            md5(b"v1\n"),
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"v2 longer\n")

    def test_existing_file_deleted_since_read(self):
        self.put("a.md", b"v1\n")
        st = self.read_state("a.md")
        (self.root / "a.md").unlink()
        rc, _, err = self.tool(
            "write", self.local("n.md", b"v2\n"), self.r("a.md"), "--base", st
        )
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    def test_shrink_guard(self):
        self.put("a.md", b"a long original body\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write", self.local("n.md", b"short\n"), self.r("a.md"), "--base", st
        )
        self.assertEqual(rc, 2)
        self.assertIn("truncation guard", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"a long original body\n")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"short\n"),
            self.r("a.md"),
            "--base",
            st,
            "--allow-shrink",
        )
        self.assertEqual(rc, 0, err)

    def test_unchanged_is_noop(self):
        self.put("a.md", b"same\n")
        st = self.read_state("a.md")
        rc, out, err = self.tool(
            "write", self.local("n.md", b"same\n"), self.r("a.md"), "--base", st
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads(out)["unchanged"])
        self.assertEqual(self.copytos(), 0)

    # duplicates (M5b, M2)
    def test_duplicate_name_blocks_read_write_move_trash(self):
        self.put("a.md", b"v1\n")
        dup = {"SHIM_DUP_NAME": "a.md"}
        rc, _, err = self.tool("read", self.r("a.md"), str(self.work / "x.md"), **dup)
        self.assertEqual(rc, 2)
        self.assertIn("same-name objects", err)
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"v2 x\n"),
            self.r("a.md"),
            "--expect-md5",
            md5(b"v1\n"),
            **dup,
        )
        self.assertEqual(rc, 2)
        self.assertIn("same-name objects", err)
        rc, _, err = self.tool("moveto", self.r("a.md"), self.r("b.md"), **dup)
        self.assertEqual(rc, 2)
        rc, _, err = self.tool(
            "trash", self.r("a.md"), CFS_DRIVE_ALLOW_NON_DRIVE="1", **dup
        )
        self.assertEqual(rc, 2)
        self.assertIn("pass --id", err)
        self.assertEqual(self.copytos(), 0)
        self.assertEqual((self.root / "a.md").read_bytes(), b"v1\n")
        self.assertFalse((self.root / "b.md").exists())

    def test_duplicate_after_upload_detected(self):
        # A retried upload that leaves a same-name twin: guards before pass, the post-write check refuses.
        self.put("a.md", b"v1\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"v1\nv2\n"),
            self.r("a.md"),
            "--base",
            st,
            "--preimage-dir",
            str(self.work / "pre"),
            SHIM_DUP_NAME="a.md",
            SHIM_DUP_AFTER_COPYTO="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("2 objects after copyto", err)
        self.assertIn("NOT rolled back", err)

    def test_lsjson_refuses_duplicates(self):
        self.put("a.md", b"v1\n")
        rc, out, err = self.tool("lsjson", f"t:{self.root}", SHIM_DUP_NAME="a.md")
        self.assertEqual(rc, 2)
        self.assertIn("duplicate names", err)
        rc, out, err = self.tool("lsjson", f"t:{self.root}")
        self.assertEqual(rc, 0, err)

    # partial failures and rollback
    def test_failed_upload_leaves_preimage(self):
        self.put("a.md", b"v1\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"v1\nv2\n"),
            self.r("a.md"),
            "--base",
            st,
            "--preimage-dir",
            str(self.work / "pre"),
            SHIM_COPYTO="fail",
        )
        self.assertEqual(rc, 2)
        self.assertIn("copyto failed", err)
        self.assertIn("nothing to roll back", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"v1\n")

    def test_short_upload_rolled_back(self):
        self.put("a.md", b"original body\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"original body\nmore\n"),
            self.r("a.md"),
            "--base",
            st,
            "--preimage-dir",
            str(self.work / "pre"),
            SHIM_COPYTO="truncate:5",
            SHIM_COPYTO_ONCE="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("rolled back", err)
        self.assertIn("verified", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"original body\n")

    def test_upload_error_after_write_rolled_back(self):
        self.put("a.md", b"original body\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"original body\nmore\n"),
            self.r("a.md"),
            "--base",
            st,
            "--preimage-dir",
            str(self.work / "pre"),
            SHIM_COPYTO="failafter",
            SHIM_COPYTO_ONCE="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("rolled back", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"original body\n")

    def test_short_upload_without_preimage_dir_still_rolled_back(self):
        # N10: the pre-image is always kept (temp folder), so rollback works without --preimage-dir.
        self.put("a.md", b"original body\n")
        st = self.read_state("a.md")
        rc, _, err = self.tool(
            "write",
            self.local("n.md", b"original body\nmore\n"),
            self.r("a.md"),
            "--base",
            st,
            SHIM_COPYTO="truncate:5",
            SHIM_COPYTO_ONCE="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("rolled back", err)
        self.assertIn("verified", err)
        self.assertEqual((self.root / "a.md").read_bytes(), b"original body\n")

    def hook(
        self,
        cmd: str,
        name: str,
        data: bytes,
        when: str = "after",
        append: bool = False,
    ):
        env = {
            "SHIM_HOOK_CMD": cmd,
            "SHIM_HOOK_WHEN": when,
            "SHIM_HOOK_PATH": str(self.root / name),
            "SHIM_HOOK_DATA": data.decode(),
            "SHIM_HOOK_DONE": str(self.tmp / "hook.done"),
        }
        if append:
            env["SHIM_HOOK_APPEND"] = "1"
        return env

    # N1: rollback never clobbers another writer (ver-4 codetest/race_rollback)
    def test_race_rollback_keeps_other_writers_edit(self):
        # B (this tool) and A read the same base. B's copyto lands, then A's upload lands
        # and A is told ok. B's post-write check sees A's body: it must report a conflict
        # and leave A's edit, not roll back to the base.
        self.put("f.md", b"base\n")
        st = self.read_state("f.md")
        a_edit = b"base\nA edit (A was told ok)\n"
        rc, _, err = self.tool(
            "write",
            self.local("f.new.md", b"base\nB edit\n"),
            self.r("f.md"),
            "--base",
            st,
            "--preimage-dir",
            str(self.work / "pre"),
            **self.hook("copyto", "f.md", a_edit.decode().encode()),
        )
        self.assertEqual(rc, 3, err)
        self.assertIn("CONFLICT", err)
        self.assertIn("NOT rolled back", err)
        self.assertEqual((self.root / "f.md").read_bytes(), a_edit)
        self.assertEqual(self.copytos(), 1)  # no rollback upload

    def test_rollback_skipped_when_short_body_is_not_ours(self):
        # A short body that is not a prefix of our bytes is someone else's: no rollback.
        self.put("f.md", b"original body\n")
        st = self.read_state("f.md")
        rc, _, err = self.tool(
            "write",
            self.local("f.new.md", b"original body\nmine\n"),
            self.r("f.md"),
            "--base",
            st,
            **self.hook("copyto", "f.md", b"theirs\n"),
        )
        self.assertEqual(rc, 3, err)
        self.assertEqual((self.root / "f.md").read_bytes(), b"theirs\n")

    def test_toctou_window_is_a_documented_limit(self):
        # A write that lands just before our copyto is overwritten and not seen by this
        # tool (README Limits); the other writer's own post-write check reports CONFLICT.
        self.put("f.md", b"base\n")
        st = self.read_state("f.md")
        rc, _, err = self.tool(
            "write",
            self.local("f.new.md", b"base\nB\n"),
            self.r("f.md"),
            "--base",
            st,
            **self.hook("copyto", "f.md", b"base\nA\n", when="before"),
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "f.md").read_bytes(), b"base\nB\n")

    def test_same_size_same_mtime_edit_is_uploaded(self):
        # N10: copyto would skip a same-size, same-modtime file; --ignore-times uploads it.
        p = self.put("f.md", b"aaaa\n")
        st = self.read_state("f.md")
        loc = Path(self.local("f.new.md", b"bbbb\n"))
        m = p.stat().st_mtime
        os.utime(loc, (m, m))
        rc, _, err = self.tool("write", str(loc), self.r("f.md"), "--base", st)
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "f.md").read_bytes(), b"bbbb\n")

    def test_text_flag_checks_a_suffixless_file(self):
        rc, _, err = self.tool(
            "write",
            self.local("script", b"#!/bin/sh\necho"),
            self.r("script"),
            "--create",
            "--text",
        )
        self.assertEqual(rc, 2)
        self.assertIn("newline", err)
        rc, _, err = self.tool(
            "write",
            self.local("script", b"#!/bin/sh\necho\n"),
            self.r("script"),
            "--create",
            "--text",
        )
        self.assertEqual(rc, 0, err)

    # N2: append
    def test_append_to_log(self):
        self.put("x.log", b"line 1\n")
        rc, out, err = self.tool(
            "append", self.local("frag.txt", b"line 2\n"), self.r("x.log")
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "x.log").read_bytes(), b"line 1\nline 2\n")
        self.assertEqual(json.loads(out)["attempts"], 1)

    def test_append_missing_needs_create(self):
        frag = self.local("frag.txt", b"first\n")
        rc, _, err = self.tool("append", frag, self.r("new.log"))
        self.assertEqual(rc, 2)
        self.assertIn("--create", err)
        rc, _, err = self.tool("append", frag, self.r("new.log"), "--create")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "new.log").read_bytes(), b"first\n")

    def test_append_refuses_cut_base_and_bad_fragment(self):
        self.put("x.log", b"line 1\nline 2 cut")
        rc, _, err = self.tool(
            "append", self.local("frag.txt", b"line 3\n"), self.r("x.log")
        )
        self.assertEqual(rc, 2)
        self.assertIn("possible truncation", err)
        self.put("y.log", b"ok\n")
        rc, _, err = self.tool(
            "append", self.local("frag.txt", b"no newline"), self.r("y.log")
        )
        self.assertEqual(rc, 2)
        self.assertEqual((self.root / "y.log").read_bytes(), b"ok\n")
        self.assertEqual(self.copytos(), 0)

    def test_append_retries_when_another_writer_appends_first(self):
        # Another agent appends right after our read: CAS refuses, we re-read and retry.
        self.put("x.log", b"line 1\n")
        rc, out, err = self.tool(
            "append",
            self.local("frag.txt", b"mine\n"),
            self.r("x.log"),
            **self.hook("cat", "x.log", b"theirs\n", append=True),
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "x.log").read_bytes(), b"line 1\ntheirs\nmine\n")
        self.assertEqual(json.loads(out)["attempts"], 2)

    def test_append_retries_after_post_write_conflict(self):
        # The ver-4 race applied to a log: their upload lands right after ours. Both lines survive.
        self.put("x.log", b"line 1\n")
        rc, out, err = self.tool(
            "append",
            self.local("frag.txt", b"mine\n"),
            self.r("x.log"),
            **self.hook("copyto", "x.log", b"line 1\ntheirs\n"),
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / "x.log").read_bytes(), b"line 1\ntheirs\nmine\n")

    def test_append_never_shrinks(self):
        # A log that shrinks between our reads is a truncation sign: stop.
        self.put("x.log", b"line 1\nline 2\n")
        rc, _, err = self.tool(
            "append",
            self.local("frag.txt", b"mine\n"),
            self.r("x.log"),
            **self.hook("cat", "x.log", b"line 1\n"),
        )
        self.assertEqual(rc, 2)
        self.assertIn("shrank", err)
        self.assertEqual((self.root / "x.log").read_bytes(), b"line 1\n")

    def test_restore_refused_off_drive(self):
        rc, _, err = self.tool("restore", f"t:{self.root}", "--id", "x")
        self.assertEqual(rc, 2)
        self.assertIn("Drive-only", err)

    # moveto (H2)
    def test_moveto_refuses_existing_destination(self):
        self.put("x-pending-t.md", b"pending body\n")
        self.put("x-complete-t.md", b"OTHER\n")
        rc, _, err = self.tool(
            "moveto", self.r("x-pending-t.md"), self.r("x-complete-t.md")
        )
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        self.assertEqual((self.root / "x-complete-t.md").read_bytes(), b"OTHER\n")
        self.assertEqual((self.root / "x-pending-t.md").read_bytes(), b"pending body\n")

    def test_moveto_ok(self):
        self.put("x-pending-t.md", b"body\n")
        rc, out, err = self.tool(
            "moveto", self.r("x-pending-t.md"), self.r("x-complete-t.md")
        )
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.root / "x-pending-t.md").exists())
        self.assertEqual((self.root / "x-complete-t.md").read_bytes(), b"body\n")
        self.assertEqual(json.loads(out)["md5"], md5(b"body\n"))

    def test_moveto_missing_source(self):
        rc, _, err = self.tool("moveto", self.r("nope.md"), self.r("b.md"))
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    # trash (H3)
    def test_trash_refused_off_drive(self):
        self.put("a.md", b"v1\n")
        rc, _, err = self.tool("trash", self.r("a.md"))
        self.assertEqual(rc, 2)
        self.assertIn("Drive-only", err)
        self.assertTrue((self.root / "a.md").exists())

    def test_trash_unique_path(self):
        self.put("a.md", b"v1\n")
        rc, out, err = self.tool("trash", self.r("a.md"), CFS_DRIVE_ALLOW_NON_DRIVE="1")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.root / "a.md").exists())

    def test_trash_wrong_id_refused(self):
        self.put("a.md", b"v1\n")
        rc, _, err = self.tool(
            "trash",
            self.r("a.md"),
            "--id",
            "someone-else",
            CFS_DRIVE_ALLOW_NON_DRIVE="1",
        )
        self.assertEqual(rc, 2)
        self.assertIn("is not an object at", err)
        self.assertTrue((self.root / "a.md").exists())

    def test_trash_missing(self):
        rc, _, err = self.tool("trash", self.r("a.md"), CFS_DRIVE_ALLOW_NON_DRIVE="1")
        self.assertEqual(rc, 2)

    def test_no_credential_access_in_source(self):
        src = TOOL.read_text()
        for bad in (
            "config dump",
            "access_token",
            "urllib",
            "Authorization",
            "config show",
        ):
            self.assertNotIn(bad, src)

    # quoting
    def test_awkward_names(self):
        name = '2026 - it\'s (a) "name" & more.md'
        rc, _, err = self.tool(
            "write", self.local("w.md", b"q\n"), self.r(name), "--create"
        )
        self.assertEqual(rc, 0, err)
        rc, _, err = self.tool("moveto", self.r(name), self.r("moved " + name))
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.root / ("moved " + name)).read_bytes(), b"q\n")

    def test_folder_name_refused(self):
        (self.root / "d.md").mkdir()
        rc, _, err = self.tool(
            "write", self.local("w.md", b"q\n"), self.r("d.md"), "--create"
        )
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
