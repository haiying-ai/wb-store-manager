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
