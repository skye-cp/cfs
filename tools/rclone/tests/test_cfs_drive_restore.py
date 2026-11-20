"""restore/trash tests against a Drive model (mockdrive.py): IDs, duplicates, trash.

Run: python3 -m unittest discover -s tools/rclone/tests -v
"""

import json
import os
import subprocess  # noqa: S404
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "cfs_drive.py"
MOCK = HERE / "mockdrive.py"
D = "gdrive:System/Admin/admin/todo"
FOLDERS = {
    "F1": ("System", "root"),
    "F2": ("Admin", "F1"),
    "F3": ("admin", "F2"),
    "F4": ("todo", "F3"),
}


class RestoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.state = tempfile.mkstemp(prefix="mockdrive_", suffix=".json")
        os.close(fd)
        self.log = self.state + ".log"
        objs = {
            i: dict(name=n, parent=p, trashed=False, isdir=True, body="")
            for i, (n, p) in FOLDERS.items()
        }
        self.save({"n": 100, "objs": objs})
        self.env = {
            **os.environ,
            "CFS_DRIVE_RCLONE": str(MOCK),
            "MOCK_STATE": self.state,
            "MOCK_LOG": self.log,
        }

    def tearDown(self):
        for p in (self.state, self.log):
            if os.path.exists(p):
                os.unlink(p)

    def load(self) -> dict:
        with open(self.state) as f:
            return json.load(f)

    def save(self, s: dict) -> None:
        with open(self.state, "w") as f:
            json.dump(s, f)

    def add(self, i, name, body="", trashed=False, parent="F4", isdir=False):
        s = self.load()
        s["objs"][i] = dict(
            name=name, parent=parent, trashed=trashed, isdir=isdir, body=body
        )
        self.save(s)

    def objs(self) -> dict:
        return {i: o for i, o in self.load()["objs"].items() if i not in FOLDERS}

    def tool(self, *args):
        p = subprocess.run(  # noqa: S603
            [sys.executable, str(TOOL), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        return p.returncode, p.stdout, p.stderr

    def calls(self) -> list:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def mutations(self) -> list:
        read_only = {"lsjson", "listremotes"}
        return [c for c in self.calls() if c[0] not in read_only]

    # ver-4 R1: a plain-trashed x.md restored after a new x.md took the name
    def test_r1_name_taken_refuses_with_no_side_effects(self):
        self.add("A", "x.md", "old\n", trashed=True)
        self.add("B", "x.md", "new\n")
        before = self.objs()
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 2, err)
        self.assertIn("is taken by ['B']", err)
        self.assertIn("nothing moved", err)
        self.assertIn("--name NEW", err)
        self.assertEqual(self.objs(), before)  # A still trashed, nothing created
        self.assertEqual(self.mutations(), [])  # listings only
        rc, _, err = self.tool("lsjson", D)
        self.assertEqual(rc, 0, err)

    def test_r1b_name_flag_restores_under_new_name(self):
        self.add("A", "x.md", "old\n", trashed=True)
        self.add("B", "x.md", "new\n")
        rc, out, err = self.tool("restore", D, "--id", "A", "--name", "x-restored.md")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["restored"], f"{D}/x-restored.md")
        o = self.objs()
        self.assertEqual(set(o), {"A", "B"})  # temp folder gone for good
        self.assertEqual((o["A"]["name"], o["A"]["trashed"]), ("x-restored.md", False))
        self.assertEqual(o["A"]["parent"], "F4")
        self.assertEqual((o["B"]["name"], o["B"]["trashed"]), ("x.md", False))

    def test_name_flag_that_is_taken_also_refuses(self):
        self.add("A", "x.md", "old\n", trashed=True)
        self.add("B", "y.md", "y\n")
        before = self.objs()
        rc, _, err = self.tool("restore", D, "--id", "A", "--name", "y.md")
        self.assertEqual(rc, 2)
        self.assertIn("taken by ['B']", err)
        self.assertEqual(self.objs(), before)
        self.assertEqual(self.mutations(), [])

    def test_name_flag_must_be_a_plain_name(self):
        self.add("A", "x.md", "old\n", trashed=True)
        rc, _, err = self.tool("restore", D, "--id", "A", "--name", "../x.md")
        self.assertEqual(rc, 2)
        self.assertIn("plain file name", err)
        self.assertEqual(self.calls(), [])

    # ver-4 R2: a trash --id suffix restored while the name is taken
    def test_r2_suffixed_name_taken_refuses(self):
        self.add("A", "x.md.cfs-trash-A", "old\n", trashed=True)
        self.add("B", "x.md", "new\n")
        before = self.objs()
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 2)
        self.assertIn("stays in trash", err)
        self.assertEqual(self.objs(), before)
        self.assertEqual(self.mutations(), [])

    # ver-4 R3 + R2-5: clean restore; other trashed items stay trashed; temp folder deleted
    def test_r3_clean_restore_leaves_others_and_no_temp_folder(self):
        self.add("A", "x.md.cfs-trash-A", "old\n", trashed=True)
        self.add("C", "y.md", "y\n", trashed=True)
        self.add("E", "z.md", "z\n", trashed=True)
        rc, out, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 0, err)
        res = json.loads(out)
        self.assertEqual((res["restored"], res["untrashed"]), (f"{D}/x.md", True))
        o = self.objs()
        self.assertEqual(
            set(o), {"A", "C", "E"}
        )  # no .cfs-restore folder, live or trashed
        self.assertEqual((o["A"]["name"], o["A"]["trashed"]), ("x.md", False))
        self.assertTrue(o["C"]["trashed"] and o["E"]["trashed"])
        rm = [c for c in self.calls() if c[0] == "rmdir"]
        self.assertEqual(len(rm), 1)
        self.assertIn("--drive-use-trash=false", rm[0])
        # a later plain untrash of the folder (ver-4 R6) brings back no temp folders
        subprocess.run(  # noqa: S603
            [sys.executable, str(MOCK), "backend", "untrash", D],
            env=self.env,
            capture_output=True,
            check=True,
        )
        self.assertEqual(set(self.objs()), {"A", "C", "E"})

    def test_r4_leftover_empty_temp_folder_is_reused_and_removed(self):
        self.add("A", "x.md", "old\n", trashed=True)
        self.add("T", ".cfs-restore-A", isdir=True)
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(self.objs()), {"A"})

    def test_temp_folder_holding_trashed_items_is_refused(self):
        # A permanent rmdir would take trashed children with it: never touch such a folder.
        self.add("A", "x.md", "old\n", trashed=True)
        self.add("T", ".cfs-restore-A", isdir=True)
        self.add("Z", "keep.md", "keep\n", trashed=True, parent="T")
        before = self.objs()
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 2)
        self.assertIn("not empty", err)
        self.assertEqual(self.objs(), before)
        self.assertEqual(self.mutations(), [])

    def test_r7_unknown_id_and_live_id_at_its_name(self):
        self.add("B", "x.md", "new\n")
        rc, _, err = self.tool("restore", D, "--id", "NOPE")
        self.assertEqual(rc, 2)
        self.assertIn("neither", err)
        rc, out, err = self.tool("restore", D, "--id", "B")
        self.assertEqual(rc, 0, err)
        self.assertFalse(json.loads(out)["untrashed"])
        self.assertEqual(self.mutations(), [])

    def test_live_suffixed_object_with_taken_name_is_not_moved(self):
        # restored by hand in the web UI, while x.md is taken: refuse, leave it as is
        self.add("A", "x.md.cfs-trash-A", "old\n")
        self.add("B", "x.md", "new\n")
        before = self.objs()
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 2)
        self.assertIn(f"stays as {D}/x.md.cfs-trash-A", err)
        self.assertEqual(self.objs(), before)

    def test_trash_then_restore_round_trip_with_duplicates(self):
        self.add("A", "x.md", "a\n")
        self.add("B", "x.md", "b\n")
        rc, out, err = self.tool("trash", f"{D}/x.md", "--id", "A")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.objs()["A"]["name"], "x.md.cfs-trash-A")
        # the name is still held by B, so restore refuses until B is out of the way
        rc, _, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 2)
        rc, _, err = self.tool("trash", f"{D}/x.md")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.tool("restore", D, "--id", "A")
        self.assertEqual(rc, 0, err)
        o = self.objs()
        self.assertEqual(set(o), {"A", "B"})
        self.assertEqual((o["A"]["name"], o["A"]["trashed"]), ("x.md", False))
        self.assertTrue(o["B"]["trashed"])


if __name__ == "__main__":
    unittest.main()
