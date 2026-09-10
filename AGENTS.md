# Codex Operating Notes For This Workspace

## Compute Budget Hygiene

- Before starting broad or multi-step work, state the task boundary and likely budget risk in one short update.
- Prefer narrow, targeted reads over broad exploration. Avoid recursive directory scans unless the user explicitly asks or the target scope is already tightly constrained.
- Keep command output small. Use summaries, counts, selected fields, `-First`, `-TotalCount`, or targeted paths instead of full JSON, full HTML, full diffs, or large directory listings.
- Do not paste or request full API documentation, whole generated payloads, or complete logs into the conversation when a summary or selected fields are enough.
- For WB listing work, default to a lightweight path: read only manifests, checklists, summaries, and the specific SKU files needed for the current step. Read image directories, official API docs, or full source files only when they are directly required.
- For long WB workflows, split analysis, code changes, media upload, backend write attempts, and failure diagnosis into separate phases. Summarize state before continuing if the context grows large.
- Before actions that may trigger approval review, consolidate the planned commands and avoid repeated approval cycles. Never place full secret tokens directly in shell commands or user-visible logs.
- When a command returns unexpectedly large output, summarize the relevant findings and avoid re-reading the same large output in later turns.
- If the task can be answered with a local artifact summary, prefer that over reopening raw source material.

## WB Finance Scheduled Runtime Immutability

- A scheduled WB finance run is production execution, not a template-development session. During that run, do not edit report builders, Python services, configuration, tests, prompts, or layout rules.
- Before any WB source call, validate the configured finance template version and the approved SHA-256 of its builder. If validation fails, stop the live run, return the latest locally verified owner report when available, and report the preflight failure.
- Revalidate the same builder fingerprint after workbook generation and before updating any publication manifest or archive. Never publish a workbook built while its template changed.
- Template, layout, formula, or preview changes must happen in a separate user-authorized development run. Run the complete automated and visual qualification there, then update the approved fingerprint as one reviewed release.
- Do not repair and repeatedly rebuild a template inside the scheduled daily run. A failed template gate is an actionable failure, not permission to mutate production code.
