---
name: console-visual
description: Use when the owner console's doorbell carries a `visual` request (the owner pressed "Request a visual" on an item) - draw it as a Mermaid diagram or a static, self-contained HTML mock with a short doc, and store it with `agent.py visual`. Called by console-process for every waiting visual request.
---

# Draw a visual the owner asked for

The owner pressed **Request a visual** on an item and said what it should
show. You answer with **one picture and a short doc**. The console server
stores the file under the project's `visuals_dir` (from `.console-kit.json`,
e.g. `architect/visuals/`), regenerates that folder's `INDEX.md`, and shows
it on the item: a Mermaid diagram as its source text, an HTML mock inside a
sandboxed frame.

Set up `KIT`, `STATE` and `A` as in the console-process skill: from the
user's registry only, and stop if this project is not registered there.

## 1. Find the request

    A view

A visual request is an owner message with `"intent": "visual"` in
`view.threads[ITEM]`; one is waiting when `view.visuals[ITEM]` holds nothing
whose `request` is that message's id. Its `text` says what to draw. If the
project sets no `visuals_dir` (`view.config.visuals_dir` is null), reply on
the item that the console has nowhere to store a visual until the owner adds
one to `.console-kit.json`, and stop.

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
- writes `<visuals_dir>/<item>/<request[:8]>-<sha256[:12]>.html` (or `.mmd`)
  byte for byte, and a `.md` doc beside it that links to it and quotes the
  owner's request;
- regenerates `<visuals_dir>/INDEX.md` from every visual in the store (it
  refuses to overwrite an `INDEX.md` it did not write);
- appends one `visual` record holding the path, size and sha256. If the file
  is later edited or swapped for a symlink, the console refuses to show it
  and says why.

A refusal names what is wrong: fix it and run the command again. A retry of
the same content is stored once.

## 5. Land it, and reply

The files are written into the console server's checkout. Land them like any
change: copy `<visuals_dir>/<item>/<name>.*` and `<visuals_dir>/INDEX.md`
into a branch and open a pull request; never commit to the main branch. A
decision-log or digest line about the visual, if the project keeps one, is
added at merge time. Then reply once on the item:

    A reply ITEM "Drew <title>: <one line>. PR <url>." --reply-to REQUEST_ID

and return to console-process.
