---
name: sync-docs
description: Audit every DPDP-POC doc that might reference a just-landed change and update the stale ones in a single pass. Run this after any commit that changes behavior, tests, or the deploy path.
---

# sync-docs — keep the Compliance Pack POC docs consistent after a change

This skill is the antidote to "I updated the code but forgot to update the
deploy guide / how-to-test / changelog." Run it as a single action after
any substantive change.

## When to run

Invoke `/dpdp:sync-docs` (or call this skill directly from a prompt) when
you've just:

- Added or renamed a script under `scripts/` or `pipelines/`
- Added a test under `tests/`
- Added or modified a UC object (table, view, UDF, mask, filter, grant)
- Changed the `bundle deploy` sequence or any prerequisite
- Introduced a new env var, workspace config, or deploy-time flag
- Closed or opened a roadmap item in `docs/changelog_and_gaps.html`

If your change is purely internal (refactor, typo fix, dead-code removal),
skip the skill.

## What the skill does

1. **Summarize the change** — in 1–2 sentences, what landed that might
   affect docs. Pull from the recent git log (`git log -3 --oneline`) and
   the current diff if any.

2. **Run the doc-audit checklist** below against the change. For each row,
   answer: does this doc currently reference the changed surface? If yes,
   is the reference still accurate? If stale, update it.

3. **Validate** — after updates, run:
   - `databricks bundle validate --target dev` (if bundle-related)
   - `python3 -c "import ast; ast.parse(open('<file>').read())"` for any
     new/changed Python file
   - HTML tag-balance check on any updated `.html` doc (the existing CI
     `html-wellformed` job in `.github/workflows/validate.yml` does this)

4. **Commit** — one commit per logical doc-sync pass, with a short
   imperative subject line. Include `[docs-sync]` as a tag in the subject.

## Doc-audit checklist

For each file below, check whether the change surfaces in it and whether
the reference is still accurate. Update only the entries that are stale.

| File | What to check |
|---|---|
| `README.md` | First-deploy sequence block, repo-layout tree, "what `phase1_bootstrap` produces" list |
| `docs/persona_deploy.md` | Prereqs section, Phase 1 deploy block, troubleshooting hints |
| `docs/changelog_and_gaps.html` | Add a `<div class="gap-card">` with LANDED/OPEN/DEFERRED badge + date; flip matching `§5.1` roadmap entry if closing a queue item |
| `docs/how_to_test.html` | Add a new `<div class="test-card">` Test N.x entry for any new UC object or behavior; update pass criteria |
| `docs/architecture.html` | The "Databricks Platform Features Used" table (around line 390); the illustrative-numbers footer; the module cards |
| `docs/persona_governance.md` | The "3-layer defense" narrative (Layer 1 / Layer 2 / Layer 3); the enforcement matrix; the "what was actually configured" sections |
| `docs/presentation.html` | Only for changes that belong in the top-level capability slides (rare — most changes don't need this) |
| `docs/business_pitch.html` | Only for numbers shown in the pitch summary table (rare) |
| `CLAUDE.md` | New env var, new file under "don't edit", new workspace prereq, any new invariant an AI should know |
| `.github/workflows/validate.yml` | Any new test file — add to `integration-tests` run step |
| `tests/` | Any ground-truth table, column, or threshold the tests assert against |

## Heuristics for "what counts as stale"

- **Deploy-sequence-affecting**: appears in README step list OR
  persona_deploy step list. Check both; they must stay in sync.
- **Test-exists claim**: if a doc says "verified by X test", `X` must
  exist in `tests/` and be wired into `.github/workflows/validate.yml`.
- **Numbers**: counts (33 PII columns, 92 gaps, 1000 events, etc.) should
  either come from the illustrative-numbers caveat section or be marked
  as such inline.
- **Workspace-specific values**: S3 paths, workspace URLs, SP names.
  Must not be hardcoded except in one loud `↓↓↓ edit this ↓↓↓` spot in
  `databricks.yml`.
- **File-layout references**: any repo-layout tree in the docs must
  match the current `ls -la` output of the directory described.

## Anti-patterns — do not do these

- Do not mass-rewrite unchanged docs "just to be safe." Only update
  entries that are actually stale.
- Do not remove date-stamped historical entries from the changelog —
  those are the audit trail. Add new entries; don't rewrite old ones.
- Do not blur the three types of numbers:
  - Regulatory ceilings (law — not disclaimed)
  - POC dataset measurements (illustrative — caveated)
  - Industry-range estimates (indicative — caveated separately)
  See `docs/business_pitch.html` footer for the canonical 3-category
  framing.

## Output template

At the end of the sync pass, summarize:

```
Change: <1-line summary>
Docs updated:
  - <file>: <one-line what changed>
  - <file>: <one-line what changed>
Docs intentionally skipped: <reason>
Validation: bundle OK / py-syntax OK / HTML OK
Committed: <short SHA> — <commit subject>
```

That summary goes in the user-visible reply so the user can verify
nothing was missed.
