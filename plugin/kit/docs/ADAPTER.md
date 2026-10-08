# The project adapter

The kit knows nothing about any one project. One Python file, the **adapter**,
is the whole seam between them. Onboarding writes a working starter to
`.overture/adapter.py` (from `adapter_template.py`); `.overture.json`'s
`fold.adapter` and `.overture/console.env`'s `CONSOLE_ADAPTER` both name it.

The server and the fold load it **by path**, so it must import on its own.

## The functions

```python
def items() -> dict[str, dict]:
    """Every piece of work the console can ask about:
    id -> {"title": str, "parent": id | None, "status": str, "section": str | None}.
    Parents make a tree; a question on a child counts toward its parents.
    `section` is an optional orchestration slug like "wave-1/phase-2/lane-ui"
    (up to 5 slash-separated segments). Overture's scaffold materialises
    each path segment as a nested <details data-ck-section="…"> on the
    dashboard, so a new item lands under its already-visible wave/phase/lane."""

def record(entries: list[dict], dry_run: bool) -> list[str]:
    """Write each folded, locked answer into the project's own record.
    Return one line per file touched. With dry_run, write nothing."""

def seed_questions() -> list[dict]:
    """Questions prepared ahead of time, as `question` records
    (qid, item, text, kind, options, star, ...), without the store's fields."""

def board() -> dict:          # OPTIONAL
    """Live values for a page that marks them with data-live* attributes:
    {"values": {key: str}, "shape": str}. Leave it out and the page is static."""
```

From 0.8.0, **fold never exports a roar transcript or a visual, on purpose**:
they are records a ruling can cite, not rulings themselves. A question a roar
(or any deliberation) produced carries `forked_from`, the fork's id, and its
entry carries that fork message whole (`fork`, with `roles: ["roar"]` for a
roar). That is enough to trace the ruling to its transcript, which is the
store's `transcript` record on the same fork id. The starter adapter prints
"Asked by: a roar panel ... (fork `<id>`)" for such a ruling; a project's own
adapter should print `forked_from` too. Visuals land in `visuals_dir` by their
own pull request, made with `agent.py visual-export` (below).

From 0.8.2, an entry whose question an agent asked under a name
(`agent.py --as agent-6`) also carries **`asked_by_agent`**: that name, which
fold checks is a plain name (lowercase letters, digits, single hyphens) before
`record()` sees it. An entry for a question asked with no name has no such
key, and is byte for byte what 0.8.1 exported. The starter adapter prints
"**Agent:** `agent-6`." only when the key is there; a project's own adapter
may print it the same way, and needs no change if it does not.

## The starter, and moving past it

The starter keeps everything under `.overture/`:

- `items.json` holds the work (`{"ID": {"title": ..., "parent": null, "section": "wave-1/phase-2/lane-ui", "status": "open"}}`);
- `seed/*.json` holds prepared questions;
- `RULINGS.md` is where locked answers are written, one section per lock.

A real project usually points these at what it already has:

- `items()` reads the project's own task list or register;
- `record()` appends to its decision log.

Keep `record()` **append-only**: the locked answers are the source of truth, and
the record is their readable history.

From 0.7.0 an entry may carry `evidence`: the question's rows
`{"cite": "path:a-b", "command"?, "result"?, "text"}`, where `text` is the
cited lines as the server read them when the question was asked. It is present
only when the question had evidence, and `fold` checks it with the store's
rules. An adapter that ignores it loses nothing else; one that writes it should
print `cite` and `command` inline and `result`/`text` as quoted blocks.

## `.overture.json`: specs, visuals and next steps (0.8.0)

Three optional keys in the project's `.overture.json`, beside `fold` and
`audit`. They are the repository's **data**: the server reads them when it
starts, and nothing ever runs a path taken from them.

```json
{
  "specs_dir": "architect/40-specs/",
  "visuals_dir": "architect/visuals/",
  "next_step": {"refine": "overture:refine", "drill": "overture:drill"}
}
```

- **`specs_dir`**: where the project keeps its specs. Read only, by the
  suggested-next-step tags beside each question and each round's answers:
  - *refine* when a locked answer cites a file under it (its `source`, an
    evidence `cite`, or a `valid_if` path) and that file has not been edited
    since the lock. "Edited" is the file's mtime in the console server,
    which starts no git (CONSOLE-kit/Q23), and the chip says the last-commit
    time is unavailable; called outside the server, `tags.compute` uses the
    last commit touching the file (`git log -1 --format=%ct`) in a git work
    tree, or the mtime when it has uncommitted changes or is untracked;
  - *drill* when the owner's own words on an answer name a term that nothing
    in the project names: any `` `backticked` `` span, or any Capitalised word
    or run of them (owner ruling "Any backtick or Capital ★"), less a fixed
    stoplist of common sentence-start and function words ("The", "We", "If"),
    and less anything found, case-insensitively, in a spec's text or file name
    under `specs_dir` or in an item's title from `items()`. Also when an item's
    status is `proposed` and no spec names its id. `overture/tags.py` has
    the exact rule, the stoplist, and its known false positives and negatives.
    Changes if: the owner finds they ignore the chip;
  - *deliberate* (needs no `specs_dir`) when the answer is stale, or its pick
    went against the ★.

  Without `specs_dir`, only *deliberate* is ever suggested.
- **`visuals_dir`** (a destination, 0.8.1): the repository folder where
  agent-drawn visuals **land by pull request**, one folder per item plus a
  generated `INDEX.md`. The server only validates this key; it never writes
  there. It stores every visual in its own state directory
  (`<state>/visuals/<item>/`, beside `store.jsonl`) and shows it from there.
  An agent lands them with

      agent.py --state STATE visual-export --project <its own worktree> [--visual ID ...]

  which copies stored visuals (by default every one; a file already there
  with the same bytes is skipped, so in effect only those not yet exported
  are written) into `<worktree>/<visuals_dir>/`, and regenerates
  `<worktree>/<visuals_dir>/INDEX.md` from what is in that folder. It
  refuses: a directory that is not the top of a git work tree; the directory
  the console server runs from (or its work tree), naming why; a different
  file already at a target path (by name; nothing is overwritten); an
  `INDEX.md` it did not generate; and any path through a symlink or onto a
  secrets file. **Without `visuals_dir`**, "Request a visual" still works and
  the visual stays viewable in the console; only the landing step needs it,
  and `visual-export` is refused by name. Keep it outside `specs_dir`.
- **`next_step`**: the skill a session runs for each kind of fork, by name.
  The console-fork skill reads it; the server only checks its shape. **The
  name is resolved ONLY against the user's installed skills** (the user-level
  skills folder, `~/.claude/skills/<name>/`, or an installed plugin's skill,
  `<plugin>:<name>`), **never against a skill folder inside the repository**:
  the repository chooses the name, so it must not also choose what the name
  runs. `agent.py next-step KIND --project DIR` does that lookup and refuses,
  naming the reason, a name that is not an installed user skill or that
  resolves into the project; the session stops there.

  The plugin ships a `refine` and a `drill` skill of its own, and onboarding
  writes `{"refine": "overture:refine", "drill": "overture:drill"}`
  when the project names none, so Refine and Drill work on a fresh install.
  They follow the "App Architect" layout (`digest.md`, `30-segments.md`,
  `40-specs/`) where a project has it, and `specs_dir` and the project's
  `CLAUDE.md` where it does not. A project with its own skills names them
  instead, as plain names (`"refine": "refine"`, a user-level skill) or as
  another plugin's `<plugin>:<name>`; onboarding never replaces a kind
  already named.

  The fourth kind of follow-up, **roar**, needs no entry: it is a panel of the
  kit's own seats, run at most **once per lock** (a second roar while the same
  lock stands is refused, naming the first; a superseded and re-locked answer
  may roar again).

Both folders must be relative paths of plain characters, at least one folder
deep, with no part starting with `.`, and must resolve inside the project (no
symlink out). A key that breaks a rule stops the server at start, by name.
Every visual path the server writes or reads is jailed the same way as
evidence: under `<state>/visuals/`, no `..`, never a secrets file, never
through a symlink (the file or any folder on the way), a regular file, at
most 256 KiB, written once, and read back only while its sha256 matches the
store's record. `visual-export` applies the same rules to the destination.

## The page

The server serves the HTML page the owner **snapshotted** (`agent.py
page-snapshot --path …`): a frozen commit stored in STATE, not the file at
`CONSOLE_PAGE` in the working tree. Legacy `CONSOLE_PAGE`/`--page` flags are
ignored since the Q28 ruling; onboarding writes a starter page and
`page-snapshot` is step 7 of the next-steps list. The server injects the
console into whatever the snapshot carries. Give each item a
`<details id="item-ID"><summary>…</summary></details>` and the console adds a
button to open that item's questions. The Inbox works on any page.

## A live board (0.9.3)

`board()` is optional. When it returns `{"shape": str, "values": {str: str}}`,
the console serves those values on every request as `view.board`. A page whose
elements carry `data-live="<key>"` picks them up without a new snapshot, so
"live" means **as fresh as the last `items-push`**: the server does not run
the adapter itself (K3, Q24).

The agent socket's `scaffold-dashboard` (0.9.3) writes a template that already
uses these attributes: Items, Rollout, Engine, Spec and a Footer, with Mermaid
rendering, favorites and the status flowchart from the panel. See the
`scaffold-dashboard` skill for the full flow. The sections live between
`<!-- scaffold:<name> start -->` / `<!-- scaffold:<name> end -->` markers so
re-running the scaffold adds missing sections without clobbering hand edits.

### Keeping it fresh

The adapter's live values re-reach the page only when `items-push` runs. Three
patterns cover almost every project:

1. **Manual push**: `agent.py --state STATE items-push --adapter ADAPTER` after
   each change the owner cares about. Fine for a slow-moving register.
2. **`items-watch`**: a long-running loop in a terminal (or a user-level
   systemd service) that re-pushes whenever the adapter,
   `.overture/items.json` or `.overture.json` changes:
       agent.py --state STATE items-watch --adapter ADAPTER
   `--interval SEC` controls the poll (default 5, clamped 1..300);
   `--watch extra/path,another` adds project-relative paths.
3. **A git post-commit hook** that runs an `items-push`. One shell line in
   `.git/hooks/post-commit`.

The scaffold's starter `board()` only populates `headline`, `status` and the
timestamp honestly; the other slots are `"—"` placeholders the owner wires to
counters the project already has (open issues from a tracker, open PRs from
the steward's `prs-push`, the latest line in the fold log). The shape key
(`overture/dashboard/1`) pins the adapter to the scaffold's layout, so a
page updated past that version can refuse an older adapter instead of
rendering half-filled.
