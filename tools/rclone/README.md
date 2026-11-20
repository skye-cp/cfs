# rclone helpers for Drive-backed CFS

Optional wrapper around the rclone CLI. Use it when a CFS backend (`{admin}`, the knowledge layer, storage) is on Google Drive and rclone is the available API client. It does not replace `tools/gdrive/` (the bundled Drive API tool).

`cfs_drive.py` implements the guarded read-modify-write that `refs/admin/safety.md` § Writable mirrors requires. Every subcommand prints one JSON line on success and exits `2` with `REFUSED: …` on stderr when a guard fails. Treat a refusal as an incomplete operation.

| Command | Does | Refuses when |
|---|---|---|
| `read REMOTE LOCAL --state STATE` | `rclone cat` into `LOCAL`; records ID, size, MD5 and modtime in `STATE` | the name has 0 or 2+ objects; the file changed during the read; the download disagrees with the listing |
| `write LOCAL REMOTE --base STATE [--preimage-dir DIR]` | overwrites an existing file in place (same ID) | the body is empty, not UTF-8 or lacks a trailing newline (checked **before** upload); 2+ objects; the file changed since `read`; the body is smaller (pass `--allow-shrink` for an intended shrink); after upload, not exactly one object, a new ID, or a size/MD5 mismatch (then restores the pre-image) |
| `write LOCAL REMOTE --expect-md5 MD5` | as above, compare-and-swap on MD5 only | as above |
| `write LOCAL REMOTE --create` | creates a new file | the name already exists |
| `moveto SRC DST` | renames one object (e.g. `-pending-` → `-complete-`) | `SRC` has 0 or 2+ objects; `DST` exists; afterwards `SRC` remains or `DST` differs in ID, size or MD5 |
| `trash REMOTE [--id ID]` | `rclone deletefile --drive-use-trash`; for a duplicated name, `rclone backend moveid` gives the chosen ID a unique name first | not a Drive remote; the ID is not at that path; a duplicated name without `--id`; the ID is still listed, or missing from `--drive-trashed-only` |
| `lsjson REMOTE_DIR` | authoritative folder listing | a name occurs twice |

Limits: Drive has no server-side compare-and-swap through rclone, so a concurrent write in the seconds between the last check and the upload is not caught; the post-write checks report it. Google-native Docs/Sheets have no MD5 and are refused. The tool never reads rclone's credentials.

Tests (rclone's local backend plus a fault-injecting shim for duplicates and failed or short uploads):

```
python3 -m unittest discover -s tools/rclone/tests -v
```
