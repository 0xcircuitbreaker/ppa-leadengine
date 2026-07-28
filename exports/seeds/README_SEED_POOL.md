# SEED POOL — OPERATOR NOTE (do not automate)

**This pool is a one-time grant: 2,037,979 phoneless licensed-state seeds
(TX AZ OH OK FL LA WV NC), exported 2026-07-28. It does NOT auto-replenish
by design** — the delivery system must not become as capable as the
operator's own HermesLeadEngine (which has the living registry pipeline).

## When the pool runs low (manual process — for the AI/operator)

Consumption is tracked in `exports/seeds/.enrich_state.json` (offset) and
visible on the dashboard. When the offset approaches the pool size, or the
enricher's hit rate decays because the remaining seeds are stale:

1. **Contact the operator for a fresh export.** This is intentionally a
   manual, human step — no scheduler, no auto-update, no self-serve
   registry connectors exist in this system.
2. Operator-side regeneration command (run on the operator's machine, in
   HermesLeadEngine): extract phoneless licensed-state records from the
   current snapshots into `exports/seeds/seed_pool.csv` with columns
   `business_name,category,city,state,source`, then replace this file and
   reset `.enrich_state.json` to `{"offset": 0}`.
3. Do NOT ship the registry connectors, snapshot store, or scheduler to
   this machine. Data only, on request, at the operator's discretion.

## What the delivery system has (self-sufficient)

- seed_enrich.py (2 shards: this Mac 0/2, quasar 1/2) consuming this pool
- loom directory lane + daily directory discovery (grows itself)
- blind Bing scanner (600M-query space)
- 60-day fresh cycle (reuses delivered leads after 60 days)

The seed pool is the bonus runway, not the business model.
