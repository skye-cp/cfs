# Todo

Create, update, or close a CFS todo as a first-class operation. Provides the canonical procedure when a todo is invoked explicitly — by the user or by an agent outside another running flow. Log every execution per `refs/admin/logging.md`.

## Scope

Manages the lifecycle of items in `{admin}/<layer>/todo/`. Operates on CFS admin state only; never mutates user data in Archive, Library, Projects, or Memory.

**This flow is for explicit, out-of-band invocations.** Other flows (file, keep, rule, encode, reflect) continue to create todos inline as part of their own execution — they know their domain context best. Dispatch here when:

- The user asks to add, update, or close a todo by itself.
- An agent needs to capture a follow-up while not running another flow.
- A todo must cross flow boundaries (e.g., a knowledge concern raised mid-storage work) and the inline path doesn't fit.

When already inside another flow's execution, that flow's "create a todo" instructions take precedence — they carry the contextual fields specific to that domain.

## Prerequisites

- Read `config.yaml` for the `admin:` address.
- See `refs/admin/safety.md` § User consent — todos are admin state (self-managed); creating, updating, or closing them does not require user consent. Mutations on user data referenced by a todo still do.
- Read `refs/admin/logging.md` § Todos for filename format, required body fields, and sensitivity rules. This flow operationalizes that spec — it does not redefine it.

## Input

- **Mode** — one of: `create`, `update`, `close`. Inferred from the trigger verb when unambiguous (e.g., *"create a todo for X"* → create; *"close todo Y"* → close).
- **Subject** (create) — what the new todo concerns.
- **Reference** (update/close) — path, filename, or descriptive title locating the existing todo.
- **Layer** — `storage`, `knowledge`, or `admin`. Required for create; inferred from the existing todo's path for update/close.
- **Body content** (create) — description, affected items, suggested next steps, per `refs/admin/logging.md`.
- **Owner** and **Waiting on** (create, update; optional) — per `refs/admin/logging.md` § Todos.
- **Update content** (update) — what to append or refine.
- **Resolution** (close) — what was done OR why the todo is no longer relevant.

## Output

- New, modified, or renamed todo file under `{admin}/<layer>/todo/`.
- Execution log in `{admin}/admin/logs/` (mandatory).

## Instructions

### Step 1: Resolve mode and layer

**Objective:** Determine which operation to perform and which layer's todo directory to act on.

1. **Mode** — from the trigger verb. If ambiguous, ask.
2. **Layer** (create mode) — route by subject:
   - Concerns Archive / Library / Projects / Unsorted contents, routing, naming, sources, or targets → **storage**.
   - Concerns Memory nodes, links, knowledge consolidation, or promotion → **knowledge**.
   - Concerns CFS admin state itself (logs, todos about todos, the CFS clone) → **admin**.
   - If the subject straddles layers, pick where the action will land. When unclear, ask.
3. **Path** — resolve `{admin}` from `config.yaml`; the todo lives at `{admin}/<layer>/todo/`.
4. For update/close: locate the existing todo file. If the reference is ambiguous (multiple matches, vague title), surface candidates and ask.

### Step 1b: Drive-backed writes (mandatory when `{admin}` is on Google Drive)

**Objective:** Every create, update, close, or bulk header edit reads and writes through the Drive API, never through a FUSE mount such as `~/gdrive`. This applies the rule in `refs/admin/safety.md` § Writable mirrors to todos.

Google Drive allows two objects with the same name in one folder, and an rclone mount can serve a stale listing or a body cut to a stale size. A close done as `mv` or `os.replace` on the mount can leave the pending object in place and can create a second complete object, and an edit based on a mount read can write a truncated body back.

Use the rclone CLI (or an equivalent Drive API client). `tools/rclone/cfs_drive.py` wraps the guarded sequence when rclone is the available CLI.

| Action | With `cfs_drive.py` | Plain rclone equivalent |
|---|---|---|
| List (authoritative) | `lsjson gdrive:…/todo/` (refuses on duplicate names) | `rclone lsjson --hash gdrive:…/todo/` |
| Read (base of an edit) | `read REMOTE LOCAL --state STATE` | `rclone lsjson --hash` then `rclone cat` |
| Create | `write LOCAL REMOTE --create` (refuses if the name exists; exit 3 if a same-name twin appears after the upload: merge the twins) | `rclone lsjson` (name absent), then `rclone copyto` |
| Update (same file ID) | `write LOCAL REMOTE --base STATE --preimage-dir DIR` | re-list and compare ID/size/MD5/modtime with your read, then `rclone copyto` |
| Rename `-pending-` → `-complete-` | `moveto SRC DST` (refuses if DST exists) | `rclone lsjson` (DST absent), then `rclone moveto` |
| Trash | `trash REMOTE` or, for a duplicated name, `trash REMOTE --id ID` (sits in trash as `<name>.cfs-trash-<ID>`) | `rclone deletefile --drive-use-trash`; for a duplicate, `rclone backend moveid` to a unique name first |
| Restore from trash | `restore FOLDER --id ID [--name NEW]` (strips the `.cfs-trash-<ID>` suffix, or uses `NEW`; if that name is taken it refuses before moving anything and the object stays in trash; deletes its empty temp folder permanently) | list the target name first (must be absent); `rclone backend untrash` restores everything trashed in a folder: move the ID into an empty folder first, then move it back and remove that folder |
| Append a line (logs) | `append FRAGMENT REMOTE [--create]` | read, append, then the Update row |

**Before writing, refuse** if the name has more than one object, if the file changed since your read, if a `-complete-` already exists for a close (merge the pending's unique content into it with a dated note, then trash the pending; see Troubleshooting for the only edits allowed on a complete), or if the body is not UTF-8, has no trailing newline, or is smaller than the current file without a reason.

**After writing, verify through the API, or refuse** (`rclone lsjson --hash`, not the mount):

- Size and MD5 match the bytes you intended to write, and an update kept the same file ID.
- After a close: the complete name has **exactly one** ID, and the pending name is gone.
- After a trash: the ID is gone from the folder and listed by `rclone lsjson --drive-trashed-only`.

If any check fails, stop, do not report the todo updated or closed, and do not continue a bulk edit. Restore the pre-image only if the file still holds your own upload (`cfs_drive.py` does this check); if it holds another writer's content (exit 3, `CONFLICT`), leave it and redo your edit on top; otherwise record the mismatch (path, expected size/md5, actual size/md5, IDs) and escalate.

Do not treat a successful local write to `~/gdrive`, or a read-back through it, as confirmation: the mount can agree with a buffer that disagrees with Drive.

### Step 2: Create

**Objective:** Write a new pending todo conforming to `refs/admin/logging.md`.

1. **Filename and body** — per `refs/admin/logging.md` § Todos. Source value for this flow is `todo flow (explicit invocation)`.
2. Write the file through the API (`rclone copyto`). Verify size, MD5 and a trailing newline per Step 1b. Do not write it through `~/gdrive`.

### Step 3: Update

**Objective:** Append to or refine an existing pending todo without changing its status.

1. Read the existing todo.
2. Apply the update — typically appending dated notes, refining the Description or Affected items, revising Suggested next steps as understanding evolves, or changing the Owner or Waiting on line.
3. Do not rename the file. Status remains `pending`.
4. Write through the API (`rclone copyto`) and verify size, MD5 and a trailing newline per Step 1b.

### Step 4: Close

**Objective:** Mark a pending todo as no longer needing future action.

1. Locate the todo. Must be in `pending` state — if already `complete`, abort with a note.
2. Append a `## Resolution` section to the body, in one of two shapes:
   - **Resolved** — timestamp, action taken, outcome. Use when the work the todo described has been done.
   - **Obsolete** — timestamp, reason no longer relevant, and a pointer to the superseding todo or state if applicable. Use when the todo was overtaken by later events.
3. Rename the file with `rclone moveto`: replace `-pending-` with `-complete-`. No other parts of the filename change. Do not `mv` or `os.replace` on a FUSE mount.
4. Verify through the API: the complete name exists as exactly one object, its size and MD5 match the body you wrote, and the pending name is gone.

### Step 5: Log

**Objective:** Record what happened.

Write a log per `refs/admin/logging.md` § Logs noting mode, layer, and the affected todo filename. For a single-action invocation, a few lines suffices.

## Examples

### Example 1: Create out of band

Input: *"Add a todo to revisit the X cost projection in two weeks — we're waiting on a vendor quote."*

Actions:
1. **Resolve** — mode=create; layer=knowledge (the projection lives in a decision-tracking node).
2. **Create** at `{admin}/knowledge/todo/YYYY-MM-DD-HHmm-pending-revisit_x_cost_projection.md` with description noting the awaiting quote and a suggested next step ("compare quoted price against the projection; update the relevant node if material").
3. **Log**.

Result: explicit todo captured; the next reflect run will surface it.

### Example 2: Close after acting (resolved)

Input: *"Close the entity-rename todo — done, folder renamed."*

Actions:
1. **Resolve** — mode=close; layer=storage; locate the matching pending todo.
2. **Close** — append Resolution (timestamp, action taken: "renamed entity folder"; outcome: "references updated, wikilinks resolve"), rename file from `-pending-` to `-complete-`.
3. **Log**.

Result: todo closed with audit trail; future scans see it as `complete`.

### Example 3: Close as obsolete

Input: *"Cancel that old reingest todo — the refresh replaced it."*

Actions:
1. **Resolve** — mode=close; layer=storage; locate the older pending todo.
2. **Close** — append Resolution (timestamp, "superseded by the refreshed todo at <new filename> which carries current scope and validated subset"), rename to `-complete-`.
3. **Log**.

Result: stale todo retired with provenance preserved.

## Troubleshooting

**Subject straddles layers**
Cause: A finding touches both a storage item and a knowledge node.
Solution: Pick the layer where the action will land. If actions are split across layers, create one todo per layer and cross-reference them in body notes.

**Existing todo not found by reference**
Cause: Reference is ambiguous, layer guess is wrong, or the todo is already `-complete-`.
Solution: List candidates by filename and ask. Modify a `-complete-` todo only to merge content from its stale pending twin (Step 1b) or to correct a stale or wrong field in its record (for example a `Waiting on` that is no longer true), each time with a dated note saying what changed and why. Never change its outcome or reopen it in place; if reopening is intended, create a new pending todo that references the closed one.

## References

| Document | When to consult |
|---|---|
| `refs/admin/logging.md` | All steps — filename format, required fields, sensitivity rules |
| `refs/admin/safety.md` | Step 1 — todos are self-managed admin state; user-data mutations referenced by a todo still require consent when acted on. Step 1b — backend API writes. |
| `tools/rclone/cfs_drive.py` | Optional rclone wrapper: guarded read, write, rename and trash that refuse on duplicates, concurrent edits, invalid bodies, or a size or MD5 mismatch |
| `refs/admin/addressing.md` | Step 1 — resolving `{admin}` and referencing affected items |
| `config.yaml` | Step 1 — `admin:` address |
| Inline-creating flows (`file`, `keep`, `rule`, `encode`, `reflect`) | When already inside one of these, prefer its inline todo guidance over dispatching here |
