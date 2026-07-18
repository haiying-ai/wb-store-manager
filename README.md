# WB Store Manager

Local toolkit for managing a Wildberries seller account through the official Seller API.

The project is intentionally conservative:

- token is read from `WB_API_TOKEN`, never hard-coded
- default operations are read-only
- snapshots are stored under `data/snapshots/`
- future write commands should require an explicit safety flag and audit log

## Quick Start

Before the first API call, create a store-scoped WB Token with the minimum required categories and configure it as an environment variable. See [WB API Token 配置](docs/wb-token-setup.md) for the exact WB Partners path, permission choices, storage location, 180-day validity and rotation procedure.

```powershell
$env:WB_API_TOKEN = "your_rotated_token"
python -m wb_manager ping
python -m wb_manager snapshot
python -m wb_manager summary
```

## Current Coverage

- API health checks across major WB domains
- content: product cards, trash, error cards, tags, card limits
- prices: goods list, quarantine goods
- marketplace: warehouses, new FBS orders, recent FBS orders
- statistics: stocks, recent orders, recent sales
- feedbacks/questions: answered and unanswered queues
- advertising: promotion count

## Security Note

The token shared earlier in chat should be rotated in the WB seller portal. Put the new token in `WB_API_TOKEN`.
