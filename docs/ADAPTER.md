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

## The page

The server serves one HTML page (`CONSOLE_PAGE`) and injects the console into
it. Give each item a `<details id="item-ID"><summary>…</summary></details>` and
the console adds a button to open that item's questions. The Inbox works on
any page.
