---
name: add-skill
description: Scaffold a new Claude Code skill into this plugin (or the current project). The user names the skill and picks a category — agent review, visualizer, documentation, other — and the matching SKILL.md template lands on disk, ready to edit.
argument-hint: <skill-name> [category]
disable-model-invocation: true
---

# Add a new skill

The user typed `/overture:add-skill $ARGUMENTS`. Walk them through creating
the skill end-to-end.

## 1. Validate the name

The first argument is the skill slug. It must match the shape
`[a-z0-9][a-z0-9._-]*` (lowercase letters, digits, dot, dash, underscore;
starts alphanumeric; up to 64 chars). If no argument was passed, OR the
first argument fails that shape, say so and stop — do not guess a name.

If the slug already exists under the target skills directory (see step 3),
tell the user and stop. Do not overwrite.

## 2. Pick a category

The second argument, if present, is the category. If absent OR it is not
one of the four values below, use the `AskUserQuestion` tool to ask:

> **"Where should this skill live? (category drives the SKILL.md template)"**

- **agent-review** — a reviewer skill (code / design / security pass)
- **visualizer** — a skill that returns a Mermaid diagram or an HTML mock
- **documentation** — a skill that writes or updates docs
- **other** — a generic skill (recommended if none of the above fit)

Record the chosen value as the `category` for step 4.

## 3. Pick the target directory

Prefer, in order:

1. `plugin/skills/<slug>/SKILL.md` — when the current working directory is
   the Overture plugin repo itself (i.e. a `plugin/skills/` directory already
   exists next to the CWD or any ancestor up to the repo root).
2. `.claude/skills/<slug>/SKILL.md` — otherwise, inside the current project's
   `.claude/` directory (create it if missing).

Tell the user which path you picked and why in one sentence.

## 4. Write SKILL.md from a category-appropriate template

Create the directory and a `SKILL.md` with YAML frontmatter at the top and
a short body. Use the template for the chosen category.

### Shared frontmatter (fill in `<slug>` from step 1)

    ---
    name: <slug>
    description: <one-sentence description of what this skill does>
    argument-hint: <ARGUMENT shape or blank>
    disable-model-invocation: true
    ---

### agent-review body

    # <slug>

    A review pass over the project's recent changes. The user typed
    `/overture:<slug> $ARGUMENTS` and wants your verdict.

    ## What to check

    1. ...
    2. ...
    3. ...

    ## How to report

    - A short summary at the top (one or two sentences).
    - A bulleted list of findings, each with: severity, where, and the fix.
    - If nothing is wrong, say so plainly.

### visualizer body

    # <slug>

    Draw a <diagram-type> for the project. The user typed
    `/overture:<slug> $ARGUMENTS`.

    ## Pick the format

    - Mermaid when the result is a flow / sequence / state chart.
    - A static HTML mock when the result is a UI shape.

    ## Return

    Call the overture visual-request path (`agent.py visual-request`) with
    the format and the body of the diagram. Do not inline the image anywhere
    else; the console renders it inside the sandboxed iframe.

### documentation body

    # <slug>

    Write or update docs about <topic>. The user typed
    `/overture:<slug> $ARGUMENTS`.

    ## Scope

    - What file(s) to touch (names them explicitly).
    - What to add vs. revise.
    - Whose voice / tone to keep.

    ## Guardrails

    - Never commit; the user reviews first.
    - Keep paragraphs short; favour lists.

### other body

    # <slug>

    A new skill. The user typed `/overture:<slug> $ARGUMENTS`.

    Fill in the goal, the inputs, the steps, and what you print back.

## 5. Confirm

In one short sentence, tell the user:

- the path you created,
- the category it was templated as,
- that the SKILL.md is a starting point and they should open it to fill the
  specifics (goal, steps, output).

Do **not** install anything, do not touch git, do not restart Claude Code.
A newly-added skill becomes available the next time the user opens a
session in this project (or re-installs the plugin, if the file landed
under `plugin/skills/`).
