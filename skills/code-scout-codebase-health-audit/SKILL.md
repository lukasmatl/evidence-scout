---
name: code-scout-codebase-health-audit
description: Runs Code Scout static discovery plus source and validation inspection to produce an evidence-backed codebase health audit. Use only for repository health audits, technical-debt assessments, risk reports, or prioritized repair plans.
---

# Code Scout Codebase Health Audit

Audit observable repository health without confusing search coverage with code
quality. Code Scout is one discovery aid; findings must come from inspected
source or executed project validation.

## Workflow

1. Define the workspace, revision or working-tree state, requested dimensions,
   exclusions, output format, and acceptance boundary. Keep deep security review
   separate unless explicitly requested.
2. Read `code-scout-cli`, ensure its index is available, and refresh it when
   missing or stale. Run at least one focused static query for the audit and one
   for each included dimension whose location or vocabulary is not already
   established. Record queries, scan status, warnings, and useful leads or
   no-match. Do not complete this skill with generic search alone. Use
   `graph inspect --format json` when extractor coverage affects the audit.
3. Read `CONTEXT.md`, domain glossaries, relevant `docs/adr/` records, ownership
   files, project metadata, and CI definitions when present. Their absence is
   unobserved context unless a bounded impact is established.
4. Map each audit dimension by combining:
   - the required static discovery pass for conceptual locations and project
     terminology;
   - `rg` and file listing for known structures and exact terms;
   - focused `fast` or `verify` only for a remaining reasoning gap.
5. Open every source range used in a finding. Locate build, test, lint,
   typecheck, dependency, and analysis commands from repository files rather
   than guessing them.
6. Run safe, relevant project-defined checks without installing, upgrading, or
   regenerating dependencies. Record command, exit status, and a bounded
   diagnostic excerpt. Mark blocked or skipped routes explicitly.
7. If a supplied execution receipt is relevant, inspect it read-only and cite
   its receipt/run ID, frozen policy, checkpoint/member scope, required-gate
   outcome, and current applicability separately. It is historical execution
   evidence, not a substitute for the required static/source discovery, and a
   scan cannot refresh its candidate or make inferred tests executed.
8. Build the evidence matrix and findings using [REFERENCE.md](REFERENCE.md).
   Search matches and counts are leads, not findings.
9. Render [REPORT-TEMPLATE.md](REPORT-TEMPLATE.md) and end with a prioritized
   `Fix this first` section. Save to the requested path, otherwise
   `docs/evidence/codebase-health-audit.md`.

Do not modify source, tests, dependencies, or configuration unless the user
separately requests implementation.

If Code Scout cannot run because the command, index preparation, or permitted
environment is unavailable, record the exact limitation prominently. Continue
only as a bounded generic audit and do not represent it as a completed Code
Scout audit.

## Evidence rules

- Cite concrete files and line ranges for architectural and behavioral facts.
- A defect requires inspected control/data flow or an observed validation
  failure; otherwise label it a likely defect, risk, or gap and name the missing
  proof.
- Keep evidence support separate from severity, confidence, and priority.
- Never infer health from file counts, static ranks, match counts, graph counts,
  warning counts, or one successful command.
- A receipt supports only its recorded historical gate outcome for its named
  checkpoint/member scope; state current applicability, integrity, and retained
  evidence completeness independently. Do not run gates merely to audit it.
- Treat “comprehensive” as coverage of declared dimensions, not every line or
  runtime path.
- Do not expose secrets or raw `.code-scout/trajectories/` content.
- Do not claim comprehensive security coverage from a general health audit.
