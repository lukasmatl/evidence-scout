# code-scout Goal

code-scout is a local-first architecture memory CLI for software workspaces.

It scans code, docs, APIs, ADRs, infra, and package metadata into an
evidence-backed memory graph. Exploration engines and inspection commands consume
that graph directly.

## User-facing surfaces

- Natural-language exploration through `code-scout -q ... --engine ...`.
- Global model routing through `code-scout config`.
- Workspace memory lifecycle through `init`, `scan`, and `watch`.
- Operational inspection through `changes` and `graph inspect`.

Generated documentation, static export formats, and semantic-query artifacts
are intentionally not public product surfaces.

## Non-goals

Legacy wiki and site-scaffold outputs are out of scope.
