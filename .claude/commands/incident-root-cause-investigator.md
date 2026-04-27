# Incident Root Cause Investigator

Master triage skill. Given an incident summary, verifies the DOS and DFS layers to determine whether the data platform is at fault, then classifies the issue and routes to the appropriate investigation sub-skill.

## Input

```
container: <CONTAINER_NUMBER>
subscription_id: <SUBSCRIPTION_ID>      (optional if journey_id provided)
journey_id: <JOURNEY_ID>               (optional if container + subscription_id provided)
expected: <what should have happened>
actual: <what actually happened>
```

$ARGUMENTS

---

## Phase A — Bind Identifiers

Parse the input above. Extract `container`, `subscription_id`, `journey_id`, `expected`, `actual`.

If `journey_id` is already provided, skip to Phase B — no query needed.

If `journey_id` is missing, load `mcp__sir-db__query` via ToolSearch and resolve it:

```sql
SELECT journey_id, status, created_on
FROM subscription
WHERE subscription_id = '<SUBSCRIPTION_ID>'
  AND unit_of_tracking::text LIKE '%<CONTAINER>%'
ORDER BY created_on DESC LIMIT 1;
```

Note the subscription `status`. If `TRACKING_STOPPED`, include that as context in the final verdict.

---

## Phase B — Verify DOS Output Layer

Load `mcp__dos-db__query` via ToolSearch. Run both queries:

```sql
-- Final enriched journey produced by data platform
SELECT id, journey_id, status, journey_type, transport_legs::text, created_on, updated_on
FROM intelligent_journey
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY updated_on DESC LIMIT 1;
```

```sql
-- Failed events DLQ — catches processing failures before DFS
SELECT id, journey_id, event_type, failure_reason, retry_count, status, created_on
FROM milestone_failed_event
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY created_on DESC LIMIT 10;
```

Compare `transport_legs` and milestone data in the result against the **expected behavior** from the incident input.

---

## Phase C — Verify DFS Guardrail Layer

DFS is the validation gateway between DOS and Consumer Platform. It consumes from `intelligentJourneyTopic`, runs all enabled rules in order (fail-fast — first failure drops the message), and publishes to `validatedIntelligentJourneyTopic` only if all pass.

### How to check

**REST API** (primary):
```
GET http://<dfs-host>/guardRail/journeyId/<JOURNEY_ID>/unitOfTracking/<CONTAINER>
```

Response comes from two DB tables:
- `validation_passed_result` — journey passed all rules; message was published downstream
- `validation_result` — journey failed a rule; `reason` column names the failing rule and why

**Alternatively:** Grafana (`tag: isce`) → DFS service → `validation.failed` / `validation.dropped` counters, or DFS pod logs.

---

### The 9 guardrail rules

All rules default to **disabled** in prod — controlled by feature flags. Rules execute in order; the first failure stops processing and drops the message.

| # | Rule | Feature Flag | What it checks |
|---|------|-------------|----------------|
| 1 | `MilestoneAfterActualEmptyContainerReturnedRule` | `MILESTONE_AFTER_ACTUAL_EMPTY_CONTAINER_RETURNED_RULE_ENABLED` | Any ACTUAL milestone (except CARRIER_RELEASE) ≥2 days **after** the latest ACTUAL EMPTY_CONTAINER_RETURNED — detects data delays post-return |
| 2 | `MilestoneBeforeActualEmptyContainerDispatchedRule` | `MILESTONE_BEFORE_ACTUAL_EMPTY_CONTAINER_DISPATCHED_RULE_ENABLED` | Any ACTUAL milestone ≥2 days **before** the earliest ACTUAL EMPTY_CONTAINER_DISPATCHED — detects premature milestones |
| 3 | `MilestoneLoadedOnVesselBeforeVesselDepartureRule` | `MILESTONE_LOADED_ON_VESSEL_BEFORE_VESSEL_DEPARTURE_RULE_ENABLED` | Latest ACTUAL LOADED_ON_VESSEL ≥2 days **after** earliest ACTUAL VESSEL_DEPARTURE on any leg — impossible loading timeline |
| 4 | `MilestoneVesselDepartureBeforeVesselArrivalRule` | `MILESTONE_VESSEL_DEPARTURE_BEFORE_VESSEL_ARRIVAL_RULE_ENABLED` | Latest ACTUAL VESSEL_DEPARTURE ≥2 days **after** earliest ACTUAL VESSEL_ARRIVAL on any leg — reversed vessel timeline |
| 5 | `MilestoneVesselArrivalBeforeUnloadedFromVesselRule` | `MILESTONE_VESSEL_ARRIVAL_BEFORE_UNLOADED_FROM_VESSEL_RULE_ENABLED` | Latest ACTUAL VESSEL_ARRIVAL ≥2 days **after** earliest ACTUAL UNLOADED_FROM_VESSEL on any leg — impossible unload timeline |
| 6 | `ShipmentDurationRule` | `SHIPMENT_DURATION_RULE_ENABLED` | Journey duration (first to last ACTUAL milestone) exceeds 9 months — unreasonably long journey |
| 7 | `OceanLegSameLocationRule` | `OCEAN_LEG_SAME_LOCATION_RULE_ENABLED` | Any ocean leg has identical start and end locations — illogical routing |
| 8 | `InvalidCircularRouteRule` | `INVALID_CIRCULAR_ROUTE_RULE_ENABLED` | Journey contains a circular pattern A→B, B→A, A→B across 3+ consecutive legs — routing loop |
| 9 | `MultiplePolPodValidationRule` | `MULTIPLE_POL_POD_VALIDATION_RULE_ENABLED` | Journey has >1 distinct Port of Loading OR >1 distinct Port of Discharge — multiple origins or destinations |

> Rules 1–5 check **ACTUAL events only** with a **2-day grace period** (≥2 days triggers failure, not exact ordering).

---

### Decision

| DFS Result | Interpretation |
|-----------|----------------|
| `validation_passed_result` row exists — message published | Proceed to Phase D verdict |
| `validation_result` row exists — message dropped | Data platform dropped the message at DFS. Read `reason` to identify the failing rule. Determine: is this a legitimate data quality issue (valid drop) or a false positive (DFS bug / disabled rule should have been off)? This is a platform finding regardless. |
| Neither table has a row | DFS has not processed this journey yet — check Kafka consumer lag on `intelligentJourneyTopic` |

---

## Phase D — Verdict Gate

| Condition | Verdict |
|-----------|---------|
| DOS output correct AND DFS guardrails all passed AND message published downstream | **Data platform is correct.** Output the evidence (query results). Declare no platform fault. Recommend upstream investigation (SCP / CP / provider). |
| DOS output incorrect OR DFS dropped or failed | **Data platform issue detected.** Proceed to Phase E to classify and route. |

---

## Phase E — Issue Classification and Sub-Skill Routing

Analyse the incident `actual` description and the DOS/DFS findings. Match against the table below and invoke the corresponding sub-skill.

| Keywords / Symptom | Sub-Skill |
|-------------------|-----------|
| "transport plan", "legs", "route", "SCP", "booking", "reconciliation", "missing leg", "extra leg", "wrong port" | `/transport-plan-investigation` |
| "vessel name", "voyage number", "wrong vessel", "wrong voyage", "vessel voyage incorrect" | `/vessel-event-investigation` with `problem: vessel_voyage` |
| "ETA", "ETD", "ATA", "ATD", "estimated arrival", "actual arrival", "estimated departure", "actual departure", "timestamp wrong" — leg sequence, vessel, and voyage all known | `/vessel-event-investigation` with `problem: timestamp` |
| "ETD stale", "ATD not received", "estimated timestamp not updating", "ETD in the past", or timestamp issue where leg/vessel/voyage unknown, or bulk containers | `/vessel-timestamp-debug` |
| "milestone", "event", "triangulation", "missing event", "wrong location" | *Milestone Missing Investigation* — not yet implemented; follow §4 of root CLAUDE.md: DOS → GIS → MCE → MP → DUST chain |
| "stitching", "container merge", "split shipment", "duplicate journey" | *Stitching Investigation* — not yet implemented; check SIR subscription split events |
| Unclear / spans multiple categories | Ask: "Is this a transport plan, vessel event, milestone, or stitching issue?" then route accordingly |

Output the classification verdict and the sub-skill invocation with the resolved identifiers passed through:

```
/transport-plan-investigation
  journey_id: <JOURNEY_ID>
  container: <CONTAINER>
  job_id: <JOB_ID if known>
```

---

## Extensibility

To add a new investigation sub-skill:
1. Create `.claude/commands/<new-skill-name>.md`
2. Add one row to the Phase E routing table above
3. Add one row to the §0 Skills table in root `CLAUDE.md`

No other changes to this file are needed.
