---
name: console-visual
description: Use when the owner console's doorbell carries a `visual` request (the owner pressed "Request a visual" on an item) - draw it as a Mermaid diagram or a static, self-contained HTML mock with a short doc, store it with `agent.py visual`, and land it by PR from your own worktree with `agent.py visual-export`. Called by console-process for every waiting visual request.
---

# Draw a visual the owner asked for

The owner pressed **Request a visual** on an item and said what it should
show. You answer with **one picture and a short doc**. The console server
stores them in its own state directory (never in the project) and shows them
on the item: a Mermaid diagram as its source text, an HTML mock inside a
sandboxed frame. To put them in the repository, you export them into **your
own worktree** and open a pull request (step 5).

Set up `KIT`, `STATE` and `A` as in the console-process skill: from the
user's registry only, and stop if this project is not registered there.

## 1. Find the request

    A todo

`todo.visuals` lists every waiting request as its `id` and `item`. A visual
request is an owner message with `"intent": "visual"`; one is waiting when
nothing in `view.visuals[ITEM]` names it as its `request`. Read its item:

    A view --item ITEM

The request is the message with that id in `view.threads[ITEM]`, and its
`text` says what to draw. If a read exits 4, run the narrower command named on stderr; do not retry the same command and do not add --full.

## 2. Choose the form

- **Mermaid** (`--format mermaid`, a `.mmd` file) for structure: a flow, a
  state machine, a sequence, a dependency map. The console shows its
  **source as text**; no diagram library runs on the owner's page. So write it
  to read well as text too: short node labels, one edge per line.
- **HTML mock** (`--format html`, one `.html` file) for layout: what a screen
  looks like at a width. It must be **self-contained and static**:
  - inline `<style>` only; images as `data:` URIs; no external URL of any
    kind (fonts, scripts, images, stylesheets): the server's policy lets it
    load nothing else, so an external asset simply does not appear;
  - **no JavaScript**. The frame is sandboxed with no scripts, and so is the
    file when it is opened on its own. A mock that needs script to show its
    states should show them side by side instead;
  - no forms that go anywhere (they are blocked), and no text pretending to
    be a login or a real product page.
- Either way, at most **256 KiB**. A larger one is refused by name, never
  cut: split it into two visuals (a request takes up to three).

Write the drawing and the doc in a scratch folder, never inside the server's
checkout.

## 3. Write the doc

A few sentences to at most 4000 characters, plain text: what the visual
shows, what it leaves out, which spec lines or files it rests on
(`path:line`), and any decision it implies that the owner has not made. A
decision it implies goes to the console as a question (console-ask), never
decided in the doc.

## 4. Store it

    A visual REQUEST_ID --format html --file grid.html --doc grid.md --title "Grid at 375 px"

`REQUEST_ID` is the request message's record id. The server:

- checks the request is an owner visual request, and the store's rules,
  **before** writing anything;
- writes `<state>/visuals/<item>/<request[:8]>-<sha256[:12]>.html` (or `.mmd`)
  byte for byte, and a `.md` doc beside it that links to it and quotes the
  owner's request. **Nothing is written into the project**;
- appends one `visual` record holding the path, size and sha256. If the file
  is later edited or swapped for a symlink, the console refuses to show it
  and says why.

A refusal names what is wrong: fix it and run the command again. A retry of
the same content is stored once. The owner can see the visual now.

## 5. Land it from your own worktree, and reply

If `view.config.visuals_dir` is null, the project has nowhere to land
visuals: skip to the reply, and say it is in the console only.

Otherwise export into **your own worktree on a branch**. **Never run it
against the server's checkout** (the directory the console server runs
from): that checkout follows main with `git merge --ff-only`, and a file
written there blocks the next fast-forward. The command refuses it anyway,
naming why.

    # from the repository YOU work in (your clone), not the server's checkout
    git fetch origin
    git worktree add <scratch>/visuals-<item> -b visuals/<item> origin/main
    A visual-export --project <scratch>/visuals-<item> --visual VISUAL_RECORD_ID

`--project` must be the TOP of that worktree, never a folder inside it.
`--visual` may repeat; without it every stored visual is exported, and one
already in the worktree with the same bytes is skipped. It exits 0 when every
chosen visual was exported, **3 when files were written but a visual was
refused** (each refusal is named on stderr: land what was written and say in
the reply which one was not), and 1 when nothing was written. It writes
`<visuals_dir>/<item>/<name>.*` and regenerates `<visuals_dir>/INDEX.md` from
what is in that folder. It refuses, writing nothing, a different file already
at a target path, or an `INDEX.md` it did not generate: look, move the file
aside if it is stale, and run it again. Then commit those paths only and open
a pull request; never commit to the main branch. A decision-log or digest
line about the visual, if the project keeps one, is added at merge time.
Reply once on the item:

    A reply ITEM "Drew <title>: <one line>. PR <url>." --reply-to REQUEST_ID

and return to console-process.
