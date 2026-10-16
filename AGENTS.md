# Repository agent guidance

<!-- review-cycle-rules:begin:v1 -->
## Code Review Rules

- Follow [.github/review-cycle/CONTRACT.md](.github/review-cycle/CONTRACT.md) alongside the existing repository and scoped instructions. Do not replace them.
- Review the authorized task against pinned approved documents and finite criteria. Cite source sections and actual code/test evidence; distinguish unverified checks.
- Independently assess fixes and rebuttals. Missing evidence, resolved comments, silence, and passing tests alone are not consensus.
- For `<!-- review-cycle-request:v1 -->`, provide the exact marked JSON verdict from the contract for the supplied full candidate SHA, pinned document revision, fingerprint and manifest; cite real source and candidate file lines.
- Both explicit independent verdicts and actual required tests/CI must match the same final SHA. Do not auto-merge, force-push, weaken checks or expand authority.
- Fix rounds have no count cap. Preserve duplicate/stale-event, failed/crashed attempt, stagnation and safety stops instead of uncontrolled retries.
- If task criteria, test readiness or product/architecture direction is unclear, ask the owner visibly on the PR, wait for an owner-scoped `/review-cycle` response or identified approved ADR/document update, and require fresh reviews.
<!-- review-cycle-rules:end:v1 -->
