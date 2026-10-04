---
name: code-scout-evidence-question-docs
description: Uses static discovery/source inspection for architecture or behavior notes, or read-only stored receipt evidence for a receipt-only question, to answer a bounded codebase question as durable, cited Markdown.
---

# Evidence Question Docs

Produce a concise document whose important repository claims are supported by
inspected source or, for a receipt-only question, validated stored execution
evidence. Code Scout helps locate evidence; it is not itself the proof.

## Investigate

1. Define the question, target workspace, intended reader, and document path.
2. Select the evidence lane. For an architecture, behavior, or current-source
   question, read `code-scout-cli` before running Code Scout commands. For a
   receipt-only question, use read-only `change show`, `change session show`,
   and/or `change evidence` through change verification instead.
3. For the source-discovery lane, ensure the local index exists and is current
   enough for the question. Run
   `init` when workspace metadata is absent and `scan` when the index is absent
   or stale; never use `init --force` implicitly.
4. In that source-discovery lane, run this required pass:
   Run at least one question-focused `--engine static` query. Do not produce a
   Code Scout evidence document from `rg` alone. Record the query, scan status,
   warnings, and useful starting points or no-match.
5. Combine the static pass with `rg` for exact symbols, strings, imports, and
   filenames, including names discovered by Code Scout.
6. Open at least one relevant returned file or cited range, and open every
   source range used to support the answer. Trace only the callers,
   tests, configuration, and data flow needed for the bounded question.
7. Use a focused `fast` or `verify` query only if direct inspection leaves a
   genuine reasoning or coverage gap. Validate its answer against files.

For a receipt-only document, sufficient stored `show`/`evidence` output may be
used without initializing an index, scanning, or running gates. Cite receipt or
run ID, session/change identity, frozen policy, checkpoint/member scope,
required-gate outcome, integrity/completeness, and current applicability. State
historical and current claims separately. A new scan cannot refresh that
candidate identity, and graph-linked or inferred test candidates never become
execution evidence without a matching recorded gate receipt.

Static text output is sufficient; require JSON only when automation needs
structured results. Do not require graph inspection or a model-backed query for
an ordinary source-discovery documentation question. A static no-match is a
retrieval gap, not proof of absence. If the required evidence route cannot run,
state the exact reason in the document rather than silently substituting generic
search.

## Keep an evidence note

Before drafting, record only what the document needs:

```md
- Question and scope: ...
- Supported claims:
  - `path/to/file.ext:10-24` — what the inspected source establishes
- Validation or tests:
  - `path/to/test.ext:30-52` — behavior exercised
- Retained execution receipt (when applicable):
  - `RUN_ID` / session or change ID; policy; checkpoint/member scope; historical
    outcome; current applicability; integrity/completeness
- Inferences: ...
- Gaps or conflicting evidence: ...
- Code Scout discovery: query, scan status, useful leads or no-match
- Search metadata that affects trust: warning, failure, or model fallback
```

Search results without inspected source remain leads. Distinguish facts,
inferences, and unknowns. If evidence is weak, narrow the answer instead of
expanding it with guesses.

## Write the document

Default to this shape unless the user asks for another:

```md
# <Question as a title>

## Answer

<Direct answer with citations.>

## How it works

<Only the relevant flow, lifecycle, data path, or ownership model.>

## Tests and safeguards

<Relevant observed coverage or explicitly bounded gaps.>

## Gaps

<Unknowns, conflicts, stale evidence, and clearly labeled inferences.>
```

Use the user's destination when provided. Otherwise prefer an existing
`docs/evidence/` directory, creating it only when the user requested a saved
artifact. Name the file with a short lowercase slug.

Every behavioral claim needs a concrete file and line citation, a scoped
execution-receipt citation, or an explicit inference label. Mention
engine/provider metadata only when it affects trust in the answer. Do not expose secrets, private endpoint details, or raw
`.code-scout/trajectories/` content.
