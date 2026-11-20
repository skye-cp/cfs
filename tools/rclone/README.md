# rclone helpers for Drive-backed CFS

Optional wrapper around the rclone CLI. Use it when a CFS backend (`{admin}`, the knowledge layer, storage) is on Google Drive and rclone is the available API client. It does not replace `tools/gdrive/` (the bundled Drive API tool).

`cfs_drive.py` implements the guarded read-modify-write that `refs/admin/safety.md` § Writable mirrors requires. Every subcommand prints one JSON line on success and exits `2` with `REFUSED: …` on stderr when a guard fails. Exit `3` with `CONFLICT: …` means another writer's content reached the file around our upload; the tool left it as it is. Treat either as an incomplete operation: re-read, redo the edit, and check the result.

| Command | Does | Refuses when |
|---|---|---|
| `read REMOTE LOCAL --state STATE` | `rclone cat` into `LOCAL`; records ID, size, MD5 and modtime in `STATE` | the name has 0 or 2+ objects; the file changed during the read; the download disagrees with the listing |
| `write LOCAL REMOTE --base STATE [--preimage-dir DIR]` | overwrites an existing file in place (same ID); the replaced body is always kept (in `DIR`, else a temp folder) | the body is empty, not UTF-8, has NUL bytes or lacks a trailing newline (checked **before** upload); 2+ objects; the file changed since `read`; the body is smaller (pass `--allow-shrink` for an intended shrink); after upload, not exactly one object, a new ID, or a size/MD5 mismatch. After a bad upload it rolls back **only its own upload**: one object, the same ID, the modtime of our upload file, and a body equal to our bytes or a prefix of them, re-checked just before the rollback. Anything else is another writer's edit: `CONFLICT`, not rolled back. |
| `write LOCAL REMOTE --expect-md5 MD5` | as above, compare-and-swap on MD5 only | as above |
| `write LOCAL REMOTE --create` | creates a new file (new episodics, todos, per-run logs) | the name already exists |
| `write … --text` / `--binary` | `--text` runs the text checks on a file without a known suffix (e.g. `scripts/gdrive-mount`); `--binary` skips them | |
| `append FRAGMENT REMOTE [--create] [--retries N]` | appends the lines in `FRAGMENT` to a log (or any text file): API read, then `write --base` of body + fragment. On a concurrent change (before or after our upload) it re-reads and appends again, up to `N` (default 5) times; if our upload landed and others appended after it, that counts as done. `--create` starts a missing file | the fragment is not clean text ending in a newline; the remote does not end with a newline (possible truncation); the remote got shorter between reads; retries run out |
| `moveto SRC DST` | renames one object (e.g. `-pending-` → `-complete-`) | `SRC` has 0 or 2+ objects; `DST` exists; afterwards `SRC` remains or `DST` differs in ID, size or MD5 |
| `trash REMOTE [--id ID]` | `rclone deletefile --drive-use-trash`; for a duplicated name, `rclone backend moveid` first renames the chosen ID to `<name>.cfs-trash-<ID>`, so **it sits in the trash under that name** | not a Drive remote; the ID is not at that path; a duplicated name without `--id`; the ID is still listed, or missing from `--drive-trashed-only` |
| `restore REMOTE_DIR --id ID [--name NAME]` | brings one trashed object back into `REMOTE_DIR` and strips a `.cfs-trash-<ID>` suffix (or renames to `NAME`). `rclone backend untrash` restores *everything* trashed in a folder and ignores filters, so the tool moves the object by ID into a fresh `.cfs-restore-<ID>` folder, untrashes there, moves it back by ID and removes the folder (Drive puts the empty folder in trash). Also renames an object already restored by hand | not a Drive remote; the ID is in neither the folder nor its trash; a folder; the temp folder exists or holds anything else; the original name is taken (then the object is restored under its trashed name and reported); size/MD5 differ afterwards |
| `lsjson REMOTE_DIR` | authoritative folder listing | a name occurs twice |

Limits:
- Drive has no server-side compare-and-swap through rclone. Another writer's upload that lands in the seconds between our last check and our upload is **overwritten, and this tool reports success** (`test_toctou_window_is_a_documented_limit`). That writer's own post-write check then sees our body and reports `CONFLICT` without rolling back, so its edit can be redone; `append` does that by itself. Keep the window small: read, edit and write in one step.
- The same gap exists between the rollback's re-check and the rollback upload.
- Drive trash keeps objects for about 30 days; keep a local backup of anything trashed that may be needed later.
- Google-native Docs/Sheets have no MD5 and are refused.
- The tool never reads rclone's credentials.

Tests (rclone's local backend plus a fault-injecting shim for duplicates and failed or short uploads):

```
python3 -m unittest discover -s tools/rclone/tests -v
```
