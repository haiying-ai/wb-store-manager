# WB Store Manager

> A local-first Wildberries operations toolkit and Codex Skill that turns seller APIs, product files, media, and operating evidence into safe, resumable workflows.

WB Store Manager helps an operator move from scattered scripts to a controlled operating loop:

`collect facts -> analyze -> prepare dry run -> approve -> execute -> read back -> audit -> archive`

It is intentionally conservative. Read-only collection is the default, sensitive values stay local, marketplace mutations require explicit scope approval, and an HTTP success is not treated as completion until the affected live state is read back.

## Why this project

Marketplace work crosses product facts, profitability, media, content, prices, FBS fulfilment, buyer questions, advertising, and reporting. The difficult part is not making one API request; it is keeping every step traceable and preventing a stale file, wrong variant, or unverified write from becoming buyer-visible.

WB Store Manager provides:

- one category knowledge base generated from source files;
- product and variant truth gates before listing work;
- profitability and WB-account-currency checks before price publication;
- dry-run-first listing, price, media, warehouse, and stock operations;
- live readback and buyer-perspective audits after publication;
- daily snapshots, funnel analysis, question review, and evidence reports;
- a reusable `wb-product-workflow` Codex Skill with four fixed workflows;
- local audit artifacts without committing tokens or private business data.

## Fixed workflows

| Workflow | Stages | Typical result |
| --- | --- | --- |
| Daily operations | collect, analyze, verify, report, archive | Prioritized store-health report with evidence |
| Listing and optimization | sync, confirm, pricing, media, validate, approve, publish, readback, buyer audit, archive | Validated and readback-confirmed sellable SKU |
| Media | facts, brief, generate, review, approve, upload, readback, archive | Approved image/video package with live verification |
| Periodic review | collect, compare, diagnose, recommend, report, archive | SKU/store decisions with confidence and next actions |

The workflow state tool makes long tasks resumable and blocks out-of-order completion:

```powershell
python .agents/skills/wb-product-workflow/scripts/workflow_state.py new `
  --flow listing `
  --task-id iphone15-blue-128 `
  --subject "iPhone 15 Blue 128 GB" `
  --output data/operations/iphone15-blue-128.json

python .agents/skills/wb-product-workflow/scripts/workflow_state.py validate `
  --file data/operations/iphone15-blue-128.json
```

The state tool records progress only. It never calls WB or bypasses approval.

## Quick start

Requirements: Python 3.12+, a WB seller token with the minimum required permissions, and Codex if you want the AI Skill.

```powershell
git clone git@github.com:haiying-ai/wb-store-manager.git
cd wb-store-manager
python -m pip install -e .
$env:WB_API_TOKEN = "your-rotated-token"
python -m wb_manager ping
python -m wb_manager snapshot
python -m wb_manager summary
```

See [WB API Token configuration](docs/wb-token-setup.md) for permission selection, Windows storage, validity, and rotation. Never place a token in a command committed to Git, a report, or a Skill file.

## Core commands

```powershell
# Store health and normalized evidence
python -m wb_manager ping
python -m wb_manager snapshot
python -m wb_manager summary

# Category knowledge base and product gates
python -m wb_manager kb sync --category 手机
python -m wb_manager product review --category 手机 --sku AI22
python -m wb_manager pricing review --category 手机 --sku AI22
python -m wb_manager listing validate --category 手机 --sku AI22

# Read live price state
python -m wb_manager pricing read --nm-id 123456789
python -m wb_manager pricing quarantine --nm-id 123456789
```

Live write commands must be reviewed as dry runs and approved for exact SKUs/nmIDs, values, and stores. After execution, keep the request, response, delayed readback, and final status together.

## What it covers

- Content: cards, trash, errors, tags, limits, characteristics, listing packages.
- Prices: live reads, upload tasks, quarantine, account-currency validation, delayed readback.
- Marketplace: warehouses, FBS orders, stock planning, exact-barcode verification.
- Analytics and statistics: stock, orders, sales, sales funnel, store snapshots.
- Customer experience: unanswered questions, buyer-risk review, post-publish audit.
- Media: staged image sets, ordering, scoring, upload planning, live verification, promo video workflow.
- Codex operations: fixed workflow routing, approval gates, resumable state, evidence archiving.

## Data layout

WB category folders live under `E:/电商运营/01平台资料/01WB/<category>`:

```text
<category>/
├── 00知识库/    # generated facts, manifests, indexes and missing facts
├── 01来源资料/  # immutable source files
├── 02生成资料/  # reviews, pricing, prompts and working media
├── 03上线资料/  # approved packages, payloads, responses and readbacks
└── 04运营资料/  # snapshots, audits and reports
```

## Safety model

1. Keep credentials in environment variables and rotate them before expiry.
2. Treat source facts and confirmed live readbacks as truth; never invent product facts or barcodes.
3. Keep variants isolated by article, barcode, price, media, and stock.
4. Require explicit approval for card, price, discount, advertising, warehouse, stock, and media mutations.
5. Require human visual review for buyer-facing media.
6. Read back the same store and sellable identity after every mutation.
7. Keep supplier terms, costs, private strategies, credentials, and raw private exports out of Git.

## Project structure

- `wb_manager/`: Seller API client, domain services, knowledge base, listing, pricing, and reporting logic.
- `.agents/skills/wb-product-workflow/`: canonical WB Codex Skill, references, assets, and workflow-state tool.
- `tests/`: unit and workflow safety tests.
- `docs/`: setup and operating documentation.
- `data/`: local snapshots and evidence; sensitive runtime artifacts are not publication material.

## Development

```powershell
python -m pytest -q
```

Reusable WB workflow changes must also be reflected in [`haiying-ai/store-manager-ru`](https://github.com/haiying-ai/store-manager-ru), which provides the multi-platform and multi-store wrapper.
