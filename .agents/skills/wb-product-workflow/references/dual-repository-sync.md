# WB Dual-Repository Sync

Reusable WB platform logic is maintained in two GitHub repositories:

- `haiying-ai/wb-store-manager`: executable WB implementation and the detailed WB product workflow Skill;
- `haiying-ai/store-manager-ru`: multi-platform/multi-store wrapper, onboarding documentation and its canonical marketplace Skill.

Whenever changing WB authentication, permissions, directory contracts, category onboarding gates, listing/media/price/stock workflows, financial reporting, scheduled financial ingestion, money-moving actions, safety requirements or readback/completion rules:

1. update the project-level package at `%WORKSPACE%\.agents\skills\wb-product-workflow`;
2. mirror that complete package to `%USERPROFILE%\.agents\skills\wb-product-workflow` and run `python .agents/skills/wb-product-workflow/scripts/sync_wb_skill_layers.py check`; zero missing or hash-mismatched project files is a release gate;
3. identify and update the corresponding documentation/Skill surface in both GitHub repositories, adapting detail rather than copying incompatible code;
4. validate both repository Skills and run relevant tests;
5. review and commit only task-related files in each repository;
6. push both repositories and report both commit IDs and the project/user skill-equivalence result.

Perform this publication sequence after every reusable Skill-logic edit, not only when the user repeats the request. Do not treat a local-only, one-layer, uncommitted, unpushed, or one-repository change as published. Store-specific secrets and live artifacts remain local and must not be mirrored.
