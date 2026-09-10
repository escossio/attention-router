# Copilot Autofix V2.1 — deterministic patch materialization

V2.1 hardens the V2 proposal boundary after the controlled persistence proof reached a safe `VALIDATION_FAILED` state because `git apply --check` rejected a model-authored unified diff.

## What changes

The Copilot proposal job no longer asks the model to author Git patch syntax.

The model may return only a bounded structured edit:

- an existing allowlisted `tests/*.py` path;
- `old_text` copied verbatim from the supplied candidate file;
- `new_text` containing the replacement;
- at most two edits;
- `HIGH` confidence for an `EDIT` decision.

Trusted Python code then:

1. verifies that each `old_text` occurs exactly once in the exact-head candidate file;
2. applies the replacement only in memory;
3. generates the unified diff with the Python standard library;
4. sends the generated diff through the existing V1.5 patch contract and budgets;
5. runs the existing ephemeral deterministic validation and cleanup;
6. only after that emits the unchanged bounded V2 proposal artifact to the separate persistence job.

## Authority is unchanged

V2.1 does not expand repository authority.

The Copilot proposal job remains `contents: read` and has no persistent repository mutation path. `contents: write` remains isolated to the deterministic `autofix-persist` job. The persistence job still has no Copilot access, performs exact-head and one-attempt checks, revalidates the final patch, uses a non-force branch update, and never merges.

## Why this is safer

The model decides the semantic replacement but cannot invent hunk line numbers, malformed diff headers, rename metadata, mode changes or other Git patch syntax. Applicability becomes a deterministic property of an exact textual match against the supplied candidate file.

The original V2 proof PR did not consume the persistent write budget: the proposal failed before persistence and remained at `0/1` writes used.
