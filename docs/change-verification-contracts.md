# Change verification contracts

The selected, frozen verification profile is the sole authority for executable
checks. Gates have stable lowercase kebab-case IDs; a receipt records each ID,
its argv, scope, dependencies, candidate digest, and observed outcome.

Story and PRD acceptance text is human guidance. Code Scout never parses that
text into commands and never accepts agent prose or command-looking output as
evidence that a gate ran. A `VERIFIED` result means only that all required gates
in one frozen profile passed for the recorded candidate and required evidence was
retained. It is not a claim of complete story acceptance, coverage, or release
approval.
