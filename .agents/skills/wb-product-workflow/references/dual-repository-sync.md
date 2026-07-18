# WB Dual-Repository Sync

Reusable WB platform logic is maintained in two GitHub repositories:

- `haiying-ai/wb-store-manager`: executable WB implementation and the detailed WB product workflow Skill;
- `haiying-ai/store-manager-ru`: multi-platform/multi-store wrapper, onboarding documentation and its canonical marketplace Skill.

Whenever changing WB authentication, permissions, directory contracts, category onboarding gates, listing/media/price/stock workflows, safety requirements or readback/completion rules:

1. identify the corresponding documentation/Skill surface in both repositories;
2. update both in the same work session, adapting detail to each repository rather than copying incompatible code;
3. validate both Skills and run relevant tests;
4. review and commit only task-related files in each repository;
5. push both repositories and report both commit IDs.

Do not treat a change as published when only one repository contains the reusable rule. Store-specific secrets and live artifacts remain local and must not be mirrored.
