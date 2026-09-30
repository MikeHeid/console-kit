# The project adapter

The kit knows nothing about any one project. One Python file, the **adapter**,
is the whole seam between them. Onboarding writes a working starter to
`.console-kit/adapter.py` (from `adapter_template.py`); `.console-kit.json`'s
`fold.adapter` and `.console-kit/console.env`'s `CONSOLE_ADAPTER` both name it.

The server and the fold load it **by path**, so it must import on its own.

## The functions

```python
def items() -> dict[str, dict]:
    """Every piece of work the console can ask about:
    id -> {"title": str, "parent": id | None, "status": str}.
    Parents make a tree; a question on a child counts toward its parents."""

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

## The starter, and moving past it

The starter keeps everything under `.console-kit/`:

- `items.json` holds the work (`{"ID": {"title": ..., "parent": null, "status": "open"}}`);
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

## `.console-kit.json`: specs, visuals and next steps (0.8.0)

Three optional keys in the project's `.console-kit.json`, beside `fold` and
`audit`. They are the repository's **data**: the server reads them when it
starts, and nothing ever runs a path taken from them.

```json
{
  "specs_dir": "architect/40-specs/",
  "visuals_dir": "architect/visuals/",
  "next_step": {"refine": "refine", "drill": "drill"}
}
```

- **`specs_dir`**: where the project keeps its specs. Read only, by the
  suggested-next-step tags beside each question and each round's answers:
  - *refine* when a locked answer cites a file under it (its `source`, an
    evidence `cite`, or a `valid_if` path) and that file has not been edited
    since the lock. "Edited" is the last commit touching the file
    (`git log -1 --format=%ct`) in a git work tree, or the file's mtime when
    it has uncommitted changes, is untracked, or the project is not in git;
  - *drill* when the owner's own words on an answer name a term (a
    `` `backticked` `` span, or a Capitalised Multi-Word phrase) that no file
    under it mentions, by text or by file name; or when an item's status is
    `proposed` and no spec names its id. `console_kit/tags.py` has the exact
    rule and its false positives and negatives;
  - *deliberate* (needs no `specs_dir`) when the answer is stale, or its pick
    went against the ★.

  Without `specs_dir`, only *deliberate* is ever suggested.
- **`visuals_dir`**: where the server writes agent-drawn visuals, one folder
  per item, plus a regenerated `INDEX.md` (it refuses to overwrite an
  `INDEX.md` it did not write). Without it, "Request a visual" says there is
  nowhere to store one, and `agent.py visual` is refused by name. Keep it
  outside `specs_dir`.
- **`next_step`**: the skill a session runs for each kind of fork, by name.
  The console-fork skill reads it; the server only checks its shape.

Both folders must be relative paths of plain characters, at least one folder
deep, with no part starting with `.`, and must resolve inside the project (no
symlink out). A key that breaks a rule stops the server at start, by name.
Every visual path the server writes or reads is jailed the same way as
evidence: under `visuals_dir`, no `..`, never a secrets file, never through a
symlink, at most 256 KiB, and read back only while its sha256 matches the
store's record.

## The page

The server serves one HTML page (`CONSOLE_PAGE`) and injects the console into
it. Give each item a `<details id="item-ID"><summary>…</summary></details>` and
the console adds a button to open that item's questions. The Inbox works on
any page.
