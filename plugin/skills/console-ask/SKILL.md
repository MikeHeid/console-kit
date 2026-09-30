---
name: console-ask
description: Use whenever work produces a question the owner must decide (a study's open questions, a lane brief's owner questions, a review trade-off) in a project registered for the owner console - post each as a lockable question so it reaches the owner's inbox, instead of leaving it in a document.
---

# Put the owner's questions on the console

A question written only in a document never reaches the owner: the console's
inbox is where they look. So when work produces a decision that is the owner's
to make, **post it** as you write it, and never tell the owner a question is
"waiting on you" until it is on the console.

Set up `KIT`, `STATE` and `A` as in the console-process skill: from the
user's registry only, and stop if this project is not registered there. In an
unregistered project, ask in the conversation instead.

## 1. Keep the document as the source

The study, brief or spec that raised the question keeps its full text. The
console carries the decision: the question, its options, the recommendation.
Each question cites its lines with `source`, so the answer can be folded back
into the document that owns it.

## 2. Write one JSON file per question

```json
{"qid": "ITEM/Q1", "item": "ITEM", "text": "What to decide, with the facts it rests on.",
 "kind": "single",
 "options": [{"id": "a", "label": "Short name", "description": "What it does and costs."},
             {"id": "b", "label": "Another", "description": "..."}],
 "star": "a", "star_by": "architect",
 "source": "path/to/doc.md:120-140",
 "valid_if": [{"kind": "excerpt", "path": "path/to/doc.md", "text": "The sentence the decision rests on."}]}
```

- `item` is the item the question belongs to; it must already exist on the
  console (`A view` lists `items`). `qid` is `<item>/Q<n>`, with `n` the next
  number unused on that item. A qid is minted once: re-asking one is refused.
- Every option has a non-empty `description` saying what it does **and what
  it costs**. `star` is the recommended option and `star_by` whose
  recommendation it is (the fields and accepted names are as in the
  console-fork skill).
- `valid_if` says what must stay true for the answer to stand. Prefer an
  **excerpt** of the lines the question rests on:
  `{"kind": "excerpt", "path": "path/to/doc.md", "text": "the exact cited sentence(s)"}`
  (8 to 4000 characters, copied from the file). It holds while that text is
  anywhere in the file, so moving it is fine and only a real edit makes the
  answer stale. Do not hash a whole big living file (`file_sha256` of a
  register, status file or log): every unrelated edit would make the answer
  stale. The full list is in the console-fork skill.
- Add **`evidence`** when the decision rests on specific lines, a command or
  a measurement (0.7.0): up to 8 rows of
  `{"cite": "path/to/file:120-134", "command": "...", "result": "..."}`.
  `cite` is required (a relative path inside the project with `:line` or
  `:start-end`, at most 200 lines); `command` (one line) and `result` (at most
  2000 characters) are optional. Never send `text`: the server reads the
  cited lines itself, so the owner's form can say later whether they changed.
  The full rules are in the console-fork skill.
- Write in plain words the owner can decide from without opening the
  document. The document's shorthand (`Q-GE5`, `D-206`) goes in the
  description only with its meaning beside it.

## 3. Post them in one call

    A ask FILE [FILE ...]

Files post in order. The first refusal stops the batch and names every file
not sent; fix that file and run again with only those. Never drop a refused
question.

## 4. Check, then tell the owner

`A view` must list every new qid under `inbox`. Only then say the questions
are on the console, naming the items they are on.
