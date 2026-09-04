# WB advertising creation and search relevance

## Scope and defaults

Read this reference for advertising recommendations, creation, resumption,
search-cluster exclusions and monitoring. Recommendations use the read-only
`review` flow. Mutations use `advertising`; deposits additionally use `funding`
and `financial-reporting.md`. Never edit SKU descriptions, images, prices or
stock as an implicit advertising optimization.

For a smartphone test, propose one SKU, manual CPM, search only, recommendations
off, and automatic recharge off. These are defaults, not proof that CPM beats
CPC or permission to change an existing campaign. A justified alternate mode
requires a corresponding approved plan. Resolve actual bids, currency, caps,
funding source, attribution windows and stop rules at runtime. Never inherit
another campaign's identifiers, exclusions, budget or historical stop-loss.

## 1. Evidence and preflight

- Resolve the exact owner store and credential reference; verify vendorCode,
  nmID, generation/series, model suffix (Pro/Pro Max/Plus/mini/Ultra etc.),
  capacity, condition, active warehouse and actual sellable stock. Missing or
  conflicting identity/stock blocks launch; never substitute another store.
- Read recent comparable funnel and advertising periods, search-cluster stats,
  existing campaigns/settings/budget, and complete order lifecycle evidence.
  Record source timestamps, period/timezone, currency, coverage and failures.
  Check for an already running or duplicate same-SKU campaign before creation.
- Select candidates using relevant traffic, carts, verified non-cancelled and
  fulfilled orders, and approved current profit policy; CTR or attributed
  orders alone is not a profitability gate. Cancellations are not realized
  revenue; unmatched ad orders are not automatically cancellations either.
- For profit calculations, validate the active pricing policy as required by
  SKILL.md. Do not claim current buyer price, imagery, warranty or delivery was
  inspected when only an older report or backend field was available.

## 2. Exact model relevance, not a global blacklist

Use the current SKU's model facts and the platform's exact returned cluster
strings. Normalize case, punctuation, whitespace and verified English/Russian
aliases for analysis only; retain the original cluster string for API writes.
Match complete model tokens, never substring digits (for example 1 versus 15).
Recognize suffix boundaries and mixed/ambiguous queries before deciding.

For each observed cluster record query, target model, classification, reason,
source period, spend/clicks/carts/orders and decision:

- `relevant`: matching model or verified alias; preserve.
- `wrong_model`: explicitly different generation or full model/series, with no
  conflicting multi-model interpretation; eligible for exclusion review.
- `accessory`: clearly accessory-only intent for a phone listing; eligible for
  exclusion review, not a naive word match such as any mention of a case.
- `broad_or_uncertain`: generic brand, incomplete model, multiple models,
  unknown alias or alternative capacity/colour; preserve pending review.

Examples are classification tests, not stored account rules: an iPhone 15
target and `iphone 12` are mismatched; `айфон 15` can be relevant; `iphone`,
`iphone 15 256` for a different-capacity iPhone 15, and `iphone 15 или 16`
must not be auto-excluded merely by a number/substring rule. For Pro Max,
an incomplete base-model query is not unambiguously another full model.

Use historical same-store/SKU clusters to prepare exclusions before launch
where supported. New campaigns may expose eligible clusters only after some
traffic; record that limitation and agree to an initial observation window.
Never claim that every wrong query can be blocked before the first impression.
Unavailable cluster data stays unavailable, not zero or an empty minus list.

## 3. Plan, approval and execution

Persist a private plan with store, SKU/nmID, campaign ID if known, evidence
cutoff, settings, bid units/currency, funding plan reference, exclusions and
reasons, exact permitted actions, and monitor policy/version. Approval must
cover exclusions as well as creation/start when those actions are intended.
Record whether later exclusion additions are authorized, their classifications
and scope. General monitoring approval is read-only unless it says otherwise.

Use official APIs for campaign writes, not browser automation. Verify the
current official promotion API contract and campaign eligibility at execution
time: <https://dev.wildberries.ru/ru/openapi/promotion>. The known cluster routes
are POST `/adv/v0/normquery/stats`, `/adv/v0/normquery/get-minus` and
`/adv/v0/normquery/set-minus`; treat their schemas/capabilities as versioned.

For an exclusion update:

1. Read the exact campaign/nmID existing list with successful source status.
   Interpret a response without that pair as unknown unless the current
   documented contract proves it explicitly means empty for the sole pair.
2. Review classifications; run `scripts/ad_exclusion_plan.py` on the private
   normalized evidence to produce a union-only dry run. It has no API client
   and does not authorize or semantically classify queries.
3. Immediately before writing, reread identity, settings and existing list.
   If the list changed, rebuild and recheck the approved scope. Preserve every
   existing term: set-minus is treated as replacement, never send an empty
   list to mean no new exclusions. A no-change plan performs no write.
4. Apply only approved additions; read back that exact pair and compare the
   complete set, plus unchanged placement/bid/SKU settings. An HTTP success
   alone is insufficient. Unknown write outcome means read-only reconciliation,
   not blind retry, rollback or overwriting another actor's change.

The dry-run input is a normalized private JSON object, not a raw API response:
`target` contains `store`, `advert_id`, `nm_id`; `snapshot` contains that same
`target`, `status: "ok"`, `complete: true`, `retrieved_at`, an `evidence` path
and explicit `norm_queries` list. Each row in `decisions` contains the same
`target`, original `query`, `classification`, `action` (`keep` or `exclude`),
`reason` and `evidence`; an exclusion additionally needs `approved: true` and
`approval_ref`. Run `python scripts/ad_exclusion_plan.py --input <private.json>`
from the skill folder. The helper verifies structural invariants only: it does
not prove evidence authenticity, freshness, model classification or actual
approval, which the executing agent must independently check.

Creation and funding also require a durable execution record/claim and fresh
duplicate checks. After uncertain creation/deposit/start, reconcile first;
never rerun a one-shot funding script. Funding source, currency and expected
debit need independent readback. Never silently use cash when promo credits
are unavailable, expired or insufficient. Only start after required identity,
settings, exclusion and funding readbacks pass, or a specifically approved
platform limitation has been recorded. If automatic recharge is not exposed
by available read APIs, say verification is unavailable; do not claim it is
confirmed off. Do not enable it implicitly.

## 4. Observation and completion

Monitoring is not supplied by this Skill alone. For a requested recurring
monitor use the Codex automation tool, inspect existing automations to avoid
duplicates, and save the actual returned automation ID. Never substitute
Windows Task Scheduler. Follow current app automation instructions.

Persist campaign-specific actions and limits in the private monitor policy:
observation frequency, source freshness, baseline, approved exclusions and any
pause authority. On each run reload that policy, verify campaign identity,
and reject stale/conflicting policy versions. If the user changes to
"spend existing budget, do not pause", update the actual scheduled prompt and
execution path before reporting the change complete; old stop rules no longer
apply. Never embed old numeric stop thresholds in reusable code.

Report cumulative and baseline-relative impressions, clicks, CTR, spend,
carts, ad-attributed orders, verified non-cancelled/fulfilled orders,
cancellations, remaining budget, settings and source completeness. Scope real
orders to the target SKU (label any group view separately) and exact launch or
resume timestamp. Missing data is not zero and cannot trigger a stop decision.
Compare conversion and profit, not just cheap clicks, when recommending scale.

New wrong-model clusters may be added only under the saved explicit authority
and the same merge/readback gates; otherwise propose them without writing.
Keep routine unchanged monitoring quiet unless periodic updates were requested.
Remove/update the actual automation when its approved completion condition is
met. Archive private evidence and show the final status; do not claim ongoing
monitoring without a successfully created task. Publishing this Skill changes
neither live campaigns nor existing scheduled prompts by itself.
