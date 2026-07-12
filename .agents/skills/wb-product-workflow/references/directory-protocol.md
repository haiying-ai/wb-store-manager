# Directory Protocol

WB category folders live under `E:\WB\<category>`.

Required folders:

- `00知识库`: generated facts, source manifest, asset index, manual overrides, missing facts, change log.
- `01来源资料`: original source files. Never modify source files in place.
- `02生成资料`: generated reviews, pricing packages, image workspaces, prompts, and external inboxes.
- `03上线资料`: confirmed listing package, WB payloads, live responses, and audit logs.
- `04运营资料`: daily operations reports and snapshots.

Keep generated batches timestamped. Promote materials into `03上线资料` only after the relevant gate passes.
