# rclone helpers for Drive-backed CFS admin

Optional wrappers around the rclone CLI. Use them when `{admin}` is on Google Drive and rclone is the available API client. They do not replace `tools/gdrive/` (the bundled Drive API tool).

`cfs_drive.py` writes, renames, lists and trashes todos (and any other Drive path) and **exits non-zero** if a post-write size or MD5 does not match the intended bytes. Flows that mutate todos must treat that as a refused completion (`flows/admin/todo.md` Step 1b).
