# Event Code Schema — PARKED (future task)

**Status:** placeholder. Detailed taxonomy decision is deferred per 2026-06-03.

## Current state (what the code enforces today)

- **`6xxxxx`** = customer complaint (any code in the range).
- **`8xxxxx`** = work done / repair / change (any code in the range).
- No sub-categorization, no auto-actioning, no parser logic.
- CHECK constraints in the DB enforce only the high-level range. See
  `migration_2026-06-03.sql` §2 / §3.
- KB entries accept either range (or NULL = generic). Manager picks freely.

## What's deferred

Designing the 2+2 (or 3+1, or whatever) sub-categorization scheme that the
parser will eventually emit. This needs a separate sit-down once Yash has:

- A clearer picture of the volume distribution of fault types across the
  first 50–100 actual fault reports submitted via the V2 UI.
- A view from field staff on how granular the repair-action codes need to
  be (too granular = friction, too coarse = useless analytics).
- A view on whether codes drive any automated action (the answer today is
  **no — purely classification**).

## Linked pivot — topup tracker

Going forward, the topup-suggester (currently in the Dispatch tab and
Manager / Heartbeat surfaces) will move to a **WO-driven** model:

- A topup is created as a **delivery WO** (existing `WO_DeliveryOrders`).
- "Time to top up next" is predicted from each machine's **consumption
  pattern** (recent vend rate, weekday vs weekend split, SKU mix) plus
  current dispenser level estimate.
- The vending-DB `MachineLookup.LastTopupTimestamp` becomes a derived/
  read-only field — source of truth is the most recent completed delivery
  WO.

That work is a separate stream; flagged here so the event-code taxonomy
doesn't get over-engineered while this larger architectural shift is in
flight.

## When this gets revisited

After ~30 days of V2 production data, Yash + I review:
1. Real distribution of fault types.
2. Whether KB suggestions are getting used (UseCount).
3. Whether manual triage time is the bottleneck.

Then we design the actual code schema, write a parser proposal, and seed
the KB.
