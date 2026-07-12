# WB Store Manager

Local toolkit for managing a Wildberries seller account through the official Seller API.

The project is intentionally conservative:

- token is read from `WB_API_TOKEN`, never hard-coded
- default operations are read-only
- snapshots are stored under `data/snapshots/`
- future write commands should require an explicit safety flag and audit log

## Quick Start

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
