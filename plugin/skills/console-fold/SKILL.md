---
name: console-fold
description: Use to turn the owner console's locked answers into the project's record - export them to committed files, preview the fold, fold, and land it through a PR. Called by console-process, or on its own.
---

# Fold locked answers into the record

An owner answer becomes a ruling only when it is **locked by the owner and
folded** (spec `owner-console.md` R1). The fold runs in two steps on purpose:
`export` copies locked answers out of the live store into files, and `fold`
records those files through the project's adapter. `fold` never reads the live
store, only files a reviewer has seen.

`KIT` and `STATE` come from **the user's registry only**, set up as in the
console-process skill; if this project is not registered there, stop.
`fold.locked`, `fold.ledger` and `fold.adapter` come from the repository's
`.console-kit.json`, and each must be a relative path of plain characters
(letters, digits, `_ . / -`) that stays inside the project: no leading `/`, no
`..`, no `~`. Refuse any other value by name. The adapter is the project's own
Python and the fold runs it, which is why the fold only ever happens in a
project the user registered.

1. **Branch.** Work on a branch of its own, never the main branch.
2. **Export.**

       python3 KIT/fold.py export --store STATE/store.jsonl --out <fold.locked>

   One file per locked question, carrying its whole locked history. If
   nothing new was written, there is nothing to fold: stop here.
3. **Read** each new file. When the owner's own words (`own_text`) change what
   the picked option means, the ruling is the words.
4. **Check the adapter before anything runs it.** Even the preview imports
   it. Compare `<fold.adapter>` and `.console-kit.json` with the main branch
   (`git diff <main> -- <fold.adapter> .console-kit.json`). If either differs,
   stop and tell the user what changed: the fold runs whatever adapter is
   checked out, and a branch you did not write could carry its own. Run every
   `fold.py` command from the project root; it refuses any path that leaves
   the project (its `--root`, default the current directory).
5. **Preview.**

       python3 KIT/fold.py fold --locked <fold.locked> --ledger <fold.ledger> \
           --adapter <fold.adapter> --dry-run

   The fold is all or nothing: one refused file refuses the lot and each
   refusal is named. Fix the cause; never hand-edit a locked file past it.
6. **Fold** with the same command without `--dry-run`. A lock already folded
   is skipped, so re-running over the whole directory is safe.
7. **Land it.** Commit the exported files, the ledger and what the adapter
   wrote, by name (never "add everything"), and open a PR following the
   project's workflow. The PR lists each folded question with the owner's pick
   and words.
