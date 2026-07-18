# Directory Protocol

`E:\电商运营` is the cross-platform materials root. WB category folders live under `E:\电商运营\01平台资料\01WB\<category>`.

At the `E:\电商运营` root, use `00共享资料` for cross-platform product facts/assets, `01平台资料\02OZON` for Ozon materials, `02店铺资料` for store assets, and `03运营数据` for normalized multi-store reports. Do not place new WB categories directly under `E:\电商运营`.

Required folders:

- `00知识库`: generated facts, source manifest, asset index, manual overrides, missing facts, change log.
- `01来源资料`: original source files. Never modify source files in place.
- `02生成资料`: generated reviews, pricing packages, image workspaces, prompts, and external inboxes.
- `03上线资料`: confirmed listing package, WB payloads, live responses, and audit logs.
- `04运营资料`: daily operations reports and snapshots.

Keep generated batches timestamped. Promote materials into `03上线资料` only after the relevant gate passes.
