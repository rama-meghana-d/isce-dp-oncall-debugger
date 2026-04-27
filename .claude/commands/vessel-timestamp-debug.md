# Vessel Timestamp Debug

Debugs missing or incorrect vessel timestamps (ATD, ETD, ATA, ETA) for one or more containers. Designed for cases where the ETD is stale (past but not updated), the ATD has not been received, or estimated timestamps are not being refreshed. Starts from DFS guardrail check, then Shipment Visibility (SV) as the authoritative source for port call timestamps, traces to IJ and MP only when needed.

## When to use this skill vs `/vessel-event-investigation`

| Scenario | Use |
|---|---|
| You know the exact leg, vessel, voyage, and location and need a full RCA | `/vessel-event-investigation` with `problem: timestamp` |
| You have a container and want to find *where* a missing or stale timestamp is stuck | **this skill** |
| Bulk check across multiple containers | **this skill** |

## Event Type Reference

Single values or comma-separated combinations are accepted:

| event_type input | event_trigger filter | event_timing filter | boundary_type |
|---|---|---|---|
| `ETD` | `VESSEL_DEPARTURE` | `ESTIMATED` | `START` |
| `ATD` | `VESSEL_DEPARTURE` | `ACTUAL` | `START` |
| `ETD,ATD` | `VESSEL_DEPARTURE` | *(none)* | `START` |
| `ETA` | `VESSEL_ARRIVAL` | `ESTIMATED` | `END` |
| `ATA` | `VESSEL_ARRIVAL` | `ACTUAL` | `END` |
| `ETA,ATA` | `VESSEL_ARRIVAL` | *(none)* | `END` |
| `ALL` or `ETD,ATD,ETA,ATA` | *(none)* | *(none)* | *(none)* |

**event_type → parse script arguments:**

| event_type | trigger arg | timing arg |
|---|---|---|
| `ETD` | `VESSEL_DEPARTURE` | `ESTIMATED` |
| `ATD` | `VESSEL_DEPARTURE` | `ACTUAL` |
| `ETD,ATD` | `VESSEL_DEPARTURE` | `ALL` |
| `ETA` | `VESSEL_ARRIVAL` | `ESTIMATED` |
| `ATA` | `VESSEL_ARRIVAL` | `ACTUAL` |
| `ETA,ATA` | `VESSEL_ARRIVAL` | `ALL` |
| `ALL` | `ALL` | `ALL` |

## SV Schema Notes (confirmed from live queries — use exactly as shown)

- **`leg_location`** — columns: `leg_id` (FK→transport_leg), `location_id`, `boundary_type` (values: `START`=departure / `END`=arrival), `vessel_voyage_id` (**nullable**), `un_loc_code`
- **`transport_leg`** — columns: `leg_id`, `journey_id` (SV internal PK), `sequence_number`, `transport_mode`, `asset_id`
- `vessel_voyage.vessel_id` = `vessel.asset_id`
- `leg_location.location_id` = `vessel_voyage_port_call.location_id` — **direct join, no bridge table needed**
- `intelligent_journey.unit_of_tracking` is a plain text column — use `=` / `IN`, no casting or LIKE needed
- `intelligent_journey` timestamp column is `updated_at` (NOT `updated_on`)
- **Fallback** (when `leg_location.vessel_voyage_id IS NULL`): use `location_un_loc_code` bridge (`un_loc_code` VARCHAR, `location_id` UUID) + vessel_name/voyage filter from IJ event references
- **`parentEventConnectionExists: false`** on `eventConnections[messageLevel=VESSEL_NAME]` in an IJ event: reliable signal that `leg_location.vessel_voyage_id` is NULL for that leg → primary SV join will miss it; fallback is required

## Primary SV Port Call Query (transport_leg → leg_location path)

Use this query when `leg_location.vessel_voyage_id` is populated (the normal case). **Always include `c.container_number IN (...)`** alongside the journey_id filter to avoid the 100-row cap silently cutting off target containers on BL-level journeys.

```sql
SELECT
  c.container_number,
  j.shipment_journey_id,
  tl.sequence_number              AS leg_seq,
  ll.boundary_type,
  ll.un_loc_code,
  v.vessel_name,
  vv.vessel_voyage_id,
  vv.voyage_number,
  vvpct.event_trigger,
  vvpct.event_timing,
  vvpct.timestamp                 AS sv_timestamp,
  vvpct.pending_timestamp,
  vvpct.source_carrier,
  vvpct.updated_at
FROM container c
JOIN journey j         ON j.shipment_unit_id  = c.shipment_unit_id
JOIN transport_leg tl  ON tl.journey_id        = j.journey_id
JOIN leg_location ll   ON ll.leg_id            = tl.leg_id
JOIN vessel_voyage vv  ON vv.vessel_voyage_id  = ll.vessel_voyage_id
JOIN vessel v          ON v.asset_id           = vv.vessel_id
JOIN vessel_voyage_port_call vvpc
  ON vvpc.vessel_voyage_id = ll.vessel_voyage_id
 AND vvpc.location_id      = ll.location_id
JOIN vessel_voyage_port_call_timestamp vvpct
  ON vvpct.port_call_id    = vvpc.id
WHERE j.shipment_journey_id IN ('<JOURNEY_ID>', ...)
  AND c.container_number IN ('<C1>', ...)
  AND tl.transport_mode = 'OCEAN'
-- AND ll.boundary_type = 'START'               ← ETD/ATD
-- AND ll.boundary_type = 'END'                 ← ETA/ATA
-- AND vvpct.event_trigger = 'VESSEL_DEPARTURE' ← ETD/ATD
-- AND vvpct.event_trigger = 'VESSEL_ARRIVAL'   ← ETA/ATA
-- AND ll.un_loc_code = '<DEPARTURE_UNLOC>'     ← if location_code_departure provided
-- AND ll.un_loc_code = '<ARRIVAL_UNLOC>'       ← if location_code_arrival provided
ORDER BY c.container_number, tl.sequence_number, ll.boundary_type, vvpct.event_trigger, vvpct.event_timing;
```

> **If no rows returned** → `leg_location.vessel_voyage_id` is NULL (SV built port call via event references, not via transport-leg path). Run `parse_ij_vessel.py` in Step 3, then use the **Fallback SV Port Call Query** below.

## Fallback SV Port Call Query (vessel + voyage + UNLOC path)

Use when the primary query returns no rows (vessel_voyage_id NULL in leg_location). Requires vessel_name, voyage_number, un_loc_code from IJ event references (output of `parse_ij_vessel.py`):

```sql
SELECT
  v.vessel_name,
  vv.vessel_voyage_id,
  vv.voyage_number,
  lul.un_loc_code,
  vvpct.event_trigger,
  vvpct.event_timing,
  vvpct.timestamp        AS sv_timestamp,
  vvpct.pending_timestamp,
  vvpct.source_carrier,
  vvpct.updated_at
FROM vessel v
JOIN vessel_voyage vv              ON vv.vessel_id          = v.asset_id
JOIN vessel_voyage_port_call vvpc  ON vvpc.vessel_voyage_id = vv.vessel_voyage_id
JOIN location_un_loc_code lul      ON lul.location_id       = vvpc.location_id
JOIN vessel_voyage_port_call_timestamp vvpct ON vvpct.port_call_id = vvpc.id
WHERE v.vessel_name    = '<VESSEL_NAME_FROM_IJ>'
  AND vv.voyage_number  = '<VOYAGE_NUMBER_FROM_IJ>'
  AND lul.un_loc_code   = '<UNLOC_FROM_IJ>'
-- AND vvpct.event_trigger = 'VESSEL_DEPARTURE'  ← ETD/ATD
-- AND vvpct.event_trigger = 'VESSEL_ARRIVAL'    ← ETA/ATA
ORDER BY vvpct.event_trigger, vvpct.event_timing, vvpct.updated_at DESC;
```

> **If still no rows with UNLOC filter:** retry without it (vessel + voyage only) to show all port calls for the voyage — identify the right port from the results.

## Input

```
container:  <CONTAINER>                        (single container — deep-dive mode)
  OR
containers: <C1>; <C2>; ...                    (bulk mode — semicolon or comma separated)

journey_id:              <UUID>                (optional — resolved from IJ if omitted)
event_type:              ETD | ATD | ETA | ATA | ETD,ATD | ETA,ATA | ALL  (required)
location_code_departure: <UNLOC>               (optional — filter ETD/ATD legs to this port)
location_code_arrival:   <UNLOC>               (optional — filter ETA/ATA legs to this port)
```

If neither location code is provided → check ALL ocean legs for the requested event_type.

$ARGUMENTS

Parse input: if `containers` is provided (multiple), run **Bulk Mode**. If single `container`, run **Single Container Deep-Dive Mode**.

---

## BULK MODE — Multiple Containers

### Bulk Step 0 — Resolve journey_ids from IJ

Load `mcp__dos-db__query` via ToolSearch. One query for all containers — IJ `unit_of_tracking` is plain text, so `IN` works directly (no JSONB casting or LIKE needed):

```sql
SELECT DISTINCT ON (unit_of_tracking)
  journey_id,
  unit_of_tracking,
  updated_at
FROM intelligent_journey
WHERE unit_of_tracking IN ('<C1>', '<C2>', ...)
ORDER BY unit_of_tracking, updated_at DESC;
```

Record `container → journey_id`. Flag containers with no IJ row — they cannot be traced further.

---

### Bulk Step 1 — DFS guardrail check

For each resolved container, load `mcp__isce-postgres-dfs__query` via ToolSearch and check:

```
GET http://<dfs-host>/guardRail/journeyId/<JOURNEY_ID>/unitOfTracking/<CONTAINER>
```

| Result | Action |
|---|---|
| `validation_result` row exists | **FLAG: DFS validation failure** — read `reason`, identify which rule fired. STOP for this container — do not investigate SV/IJ further. |
| `validation_passed_result` row exists | Note "DFS: PASSED". Continue to Step 2. |
| Neither row | Note "DFS: not yet processed". Continue to Step 2. |

Containers with a DFS failure are excluded from Steps 2–4 and reported in the final output with the failing rule and reason.

---

### Bulk Step 2 — Fetch SV port call timestamps for all containers

Load `mcp__shipment-visibility-db__query` via ToolSearch.

Run the **Primary SV Port Call Query** for all containers that passed DFS check. Apply event_type and location code filters:
- ETD or ATD or ETD,ATD → add `AND ll.boundary_type = 'START' AND vvpct.event_trigger = 'VESSEL_DEPARTURE'`
- ETA or ATA or ETA,ATA → add `AND ll.boundary_type = 'END' AND vvpct.event_trigger = 'VESSEL_ARRIVAL'`
- ALL → no boundary_type or trigger filter
- `location_code_departure` provided → add `AND ll.un_loc_code = '<DEPARTURE_UNLOC>'` (applies only to START rows)
- `location_code_arrival` provided → add `AND ll.un_loc_code = '<ARRIVAL_UNLOC>'` (applies only to END rows)

> **If a container returns no rows** (vessel_voyage_id NULL for its legs): note it as a fallback candidate — will be handled in Step 3 after IJ is fetched.

**Per leg, per event_type — classify each row:**

| Condition | Classification |
|---|---|
| `pending_timestamp` non-null | `PENDING_NEEDS_PROMOTION` |
| `sv_timestamp` present, no pending | `SV_HAS_TIMESTAMP` → compare with IJ in Step 3 |
| No SV rows for container | `NO_PORTCALL_IN_SV` → requires IJ fallback path in Step 3 |

---

### Bulk Step 3 — IJ comparison via parse_ij_vessel.py

Query IJ for all containers that have `SV_HAS_TIMESTAMP` or `NO_PORTCALL_IN_SV` classification (skip `PENDING_NEEDS_PROMOTION` — those are done):

```sql
SELECT DISTINCT ON (unit_of_tracking)
  journey_id, unit_of_tracking, journey::text, updated_at
FROM intelligent_journey
WHERE unit_of_tracking IN ('<C1>', '<C2>', ...)
  AND journey_id IN ('<JOURNEY_ID_1>', '<JOURNEY_ID_2>', ...)
ORDER BY unit_of_tracking, updated_at DESC;
```

> **IJ JSON is large.** Save results to file, then parse with both scripts:
> ```bash
> # Event timestamps:
> python3 scripts/parse_ij.py <file> <trigger> <timing> [un_loc_code]
>
> # Vessel/voyage from event references (authoritative — NOT from leg.vessel):
> python3 scripts/parse_ij_vessel.py <file> <event_type>
> ```

`parse_ij_vessel.py` outputs per ocean leg:
- `vessel_name`, `voyage_number` from the matching event's `references[]`
- `sv_link: YES/NO` — whether `leg_location.vessel_voyage_id` is set (parentEventConnectionExists)
- `→ SV fallback:` line — ready-to-use vessel/voyage/unloc values for the Fallback SV Port Call Query

**For `NO_PORTCALL_IN_SV` containers:** if `sv_link: NO` is shown by `parse_ij_vessel.py`, run the **Fallback SV Port Call Query** using the printed `vessel='...' voyage='...' unloc='...'` values. Evaluate the fallback result using the Step 2 classification table before continuing.

**Compare SV vs IJ per leg per event_type:**

| Finding | Classification | Action |
|---|---|---|
| SV timestamp == IJ timestamp (date-level) | `SV_IJ_IN_SYNC` | Proceed to Step 4 (MP) |
| SV timestamp ≠ IJ timestamp | `SV_SYNC_ISSUE` | SV has wrong/stale value vs IJ. Flag. STOP. |
| IJ timestamp missing (event not in IJ) | `IJ_MISSING_EVENT` | Proceed to Step 4 (MP) |
| IJ journey missing entirely | `NO_IJ_RECORD` | Platform issue. Flag. STOP. |

---

### Bulk Step 4 — Milestone Processor (STOP here)

For containers classified `SV_IJ_IN_SYNC` or `IJ_MISSING_EVENT`, load `mcp__milestone-processor-db__query` via ToolSearch:

```sql
SELECT smm.job_id, smm.unit_of_tracking, smm.journey_leg_id, smm.status,
       m.enriched_data::text, m.updated_at
FROM subscription_milestone_metadata smm
JOIN milestone m ON m.job_id = smm.job_id AND m.unit_of_tracking = smm.unit_of_tracking
WHERE smm.journey_id IN ('<JOURNEY_ID_1>', ...)
  AND smm.unit_of_tracking IN ('<C1>', ...)
ORDER BY smm.unit_of_tracking, m.updated_at DESC;
```

> `smm.status` is smallint — do NOT filter `= 'ACTIVE'`.

If result saved to file: `python3 scripts/parse_mp.py <file> <trigger> <timing> [unloc]`

| Finding | Classification | Action |
|---|---|---|
| MP timestamp == IJ timestamp | `ALL_LAYERS_IN_SYNC` | No platform issue — escalate to CP |
| MP timestamp ≠ IJ timestamp | `MP_TO_IJ_PROPAGATION_ISSUE` | Flag to DI engineering |
| MP has no event | `PROVIDER_NOT_SENT_TO_MP` | Check DUST/DAS (manual escalation step) |

**STOP after MP. Do not proceed to DUST or DAS in this flow.**

---

### Bulk Output

**Table 1 — Summary**

| Container | Journey ID | DFS | Vessel | Voyage | UNLOC | SV Timestamp | SV Pending | IJ Timestamp | MP Timestamp | Status | Action |
|---|---|---|---|---|---|---|---|---|---|---|---|
| TIIU4349715 | f97841f9-... | PASSED | CMA CGM SYRACUSE | 0FFW4E1MA | INMUN | null | 2026-04-23 | 2026-04-23 | — | PENDING_NEEDS_PROMOTION | Promote pending in SV UI |
| ... | | | | | | | | | | | |

**Table 2 — Per-container RCA** — one block per container where status is not `ALL_LAYERS_IN_SYNC`, using the RCA Report format at the end of this skill.

---

## SINGLE CONTAINER DEEP-DIVE MODE

### Step 0 — Resolve journey_id

**Skip if `journey_id` already provided.**

Load `mcp__dos-db__query` via ToolSearch. Query IJ directly — faster than SIR's JSONB LIKE scan:

```sql
SELECT DISTINCT ON (unit_of_tracking)
  journey_id, unit_of_tracking, updated_at
FROM intelligent_journey
WHERE unit_of_tracking = '<CONTAINER>'
ORDER BY unit_of_tracking, updated_at DESC;
```

Record `journey_id`. If no row → no IJ record exists; flag as platform issue and stop.

---

### Step 1 — DFS guardrail check (FIRST — before SV)

```
GET http://<dfs-host>/guardRail/journeyId/<JOURNEY_ID>/unitOfTracking/<CONTAINER>
```

| Result | Action |
|---|---|
| `validation_result` row exists | **FLAG: DFS validation failure** — read `reason`, identify which rule fired. Output the RCA Report with DFS FAILED. STOP — do not investigate SV/IJ further. |
| `validation_passed_result` row exists | Note "DFS: PASSED". Continue to Step 2. |
| Neither row | Note "DFS: not yet processed". Continue to Step 2. |

---

### Step 2 — Fetch SV port call timestamps

Load `mcp__shipment-visibility-db__query` via ToolSearch.

Run the **Primary SV Port Call Query** with:
- `j.shipment_journey_id IN ('<JOURNEY_ID>')`
- `c.container_number IN ('<CONTAINER>')`
- Apply `boundary_type` and `event_trigger` filters based on `event_type` input (omit both for ALL)
- Apply location code filters if `location_code_departure` or `location_code_arrival` were provided

**If no rows returned** → `leg_location.vessel_voyage_id` is NULL for this journey. Proceed to Step 3 to fetch IJ and run `parse_ij_vessel.py`, then execute the **Fallback SV Port Call Query** with the extracted vessel/voyage/UNLOC values.

**Per leg, per event_type — classify:**

| Condition | Classification |
|---|---|
| `pending_timestamp` non-null | `PENDING_NEEDS_PROMOTION` → output RCA and STOP |
| `sv_timestamp` present, no pending | `SV_HAS_TIMESTAMP` → proceed to Step 3 (IJ comparison) |
| No rows at all | `NO_PORTCALL_IN_SV` → proceed to Step 3 to extract vessel/voyage for fallback |

If `PENDING_NEEDS_PROMOTION` — output the RCA Report and **stop here**:

> **Root cause:** Pending port call update exists and has not been promoted.
> **Action:** Promote via SV UI using `vessel_voyage_id` + the port call row.
> **Evidence:** `pending_timestamp` = `<value>`, committed `sv_timestamp` = `<current value>`.

---

### Step 3 — IJ comparison via parse_ij_vessel.py

If IJ was already fetched in Step 0 (journey_id was resolved there), re-use that result. Otherwise query now:

```sql
SELECT journey_id, unit_of_tracking, journey::text, updated_at
FROM intelligent_journey
WHERE journey_id       = '<JOURNEY_ID>'
  AND unit_of_tracking = '<CONTAINER>'
ORDER BY updated_at DESC LIMIT 1;
```

If no IJ row → flag `NO_IJ_RECORD` as platform issue and stop.

**If result is saved to file**, run both scripts:
```bash
# Event timestamps:
python3 scripts/parse_ij.py <file> <trigger> <timing> [un_loc_code]

# Vessel/voyage per leg from event references (NOT from leg.vessel):
python3 scripts/parse_ij_vessel.py <file> <event_type>
```

`parse_ij_vessel.py` shows per ocean leg:
- `vessel_name`, `voyage_number` from matching event's `references[]`
- `sv_link: YES/NO` (parentEventConnectionExists on VESSEL_NAME eventConnection)
- `→ SV fallback:` line with vessel/voyage/unloc values for the Fallback SV Port Call Query

**If `sv_link: NO`** on any leg → primary SV query path misses this leg. Run Fallback SV Port Call Query using the `→ SV fallback:` values, then classify the fallback result using Step 2 before continuing.

**Compare SV vs IJ per leg per event_type:**

| Finding | Classification | Action |
|---|---|---|
| SV timestamp == IJ timestamp (date-level) | `SV_IJ_IN_SYNC` | Proceed to Step 4 (MP) |
| SV timestamp ≠ IJ timestamp | `SV_SYNC_ISSUE` | SV has wrong/stale value vs IJ. Output RCA. STOP. |
| IJ timestamp missing (event not in IJ) | `IJ_MISSING_EVENT` | Proceed to Step 4 (MP) to see if MP has it |
| IJ journey missing entirely | `NO_IJ_RECORD` | Platform issue. Output RCA. STOP. |

---

### Step 4 — Milestone Processor (STOP here)

Load `mcp__milestone-processor-db__query` via ToolSearch:

```sql
SELECT smm.job_id, smm.unit_of_tracking, smm.journey_leg_id, smm.status,
       m.enriched_data::text, m.updated_at
FROM subscription_milestone_metadata smm
JOIN milestone m
  ON m.job_id          = smm.job_id
 AND m.unit_of_tracking = smm.unit_of_tracking
WHERE smm.journey_id       = '<JOURNEY_ID>'
  AND smm.unit_of_tracking  = '<CONTAINER>'
ORDER BY m.updated_at DESC;
```

> **Note:** `smm.status` is a smallint — do NOT filter `= 'ACTIVE'`.

**If result is saved to file**, parse with:
```bash
python3 scripts/parse_mp.py <saved_file> <trigger> <timing> [un_loc_code]
```

| Finding | Classification | Action |
|---|---|---|
| MP timestamp == IJ timestamp | `ALL_LAYERS_IN_SYNC` | No platform issue — escalate to CP |
| MP timestamp ≠ IJ timestamp | `MP_TO_IJ_PROPAGATION_ISSUE` | Provide MP value (correct) vs IJ value (wrong); flag to DI engineering |
| MP has no event | `PROVIDER_NOT_SENT_TO_MP` | Check DUST/DAS (manual escalation step outside this skill) |

**STOP after MP. Do not proceed to DUST or DAS in this flow.**

---

## RCA Report

```
═══════════════════════════════════════════════════════
VESSEL TIMESTAMP DEBUG REPORT
═══════════════════════════════════════════════════════

INPUT
  container              : <CONTAINER>
  journey_id             : <JOURNEY_ID>  (resolved from IJ / provided)
  event_type             : <ETD,ATD | ETA,ATA | ALL | ...>
  location_code_departure: <UNLOC or "all ocean legs">
  location_code_arrival  : <UNLOC or "all ocean legs">

DFS GUARDRAIL
  status                 : PASSED | FAILED | NOT_YET_PROCESSED
  [if FAILED:]
  failing_rule           : <rule name>
  reason                 : <reason string>

───────────────────────────────────────────────────────
PER-LEG VALUES
───────────────────────────────────────────────────────

SV — leg <N>  (<start_unloc> → <end_unloc>)
  vessel_name     : <from SV vessel table via vessel_voyage_id>
  voyage_number   : <value>
  vessel_voyage_id: <UUID | "NULL — used event-ref fallback">
  sv_link         : YES | NO  (parentEventConnectionExists in IJ events)
  ETD sv_timestamp: <value or null>   [PENDING / CORRECT / STALE / MISSING]
  ATD sv_timestamp: <value or null>   [PENDING / CORRECT / STALE / MISSING]
  ETA sv_timestamp: <value or null>   [PENDING / CORRECT / STALE / MISSING]
  ATA sv_timestamp: <value or null>   [PENDING / CORRECT / STALE / MISSING]
  pending ETD     : <value or null>
  pending ATD     : <value or null>
  pending ETA     : <value or null>
  pending ATA     : <value or null>
  source_carrier  : <value>
  sv_updated_at   : <value>

IJ — leg <N>  (from event references via parse_ij_vessel.py)
  vessel_name     : <from event references — NOT transport plan>
  voyage_number   : <value>
  IJ ETD          : <value or "missing">  [MATCH / MISMATCH vs SV]
  IJ ATD          : <value or "missing">  [MATCH / MISMATCH vs SV]
  IJ ETA          : <value or "missing">  [MATCH / MISMATCH vs SV]
  IJ ATA          : <value or "missing">  [MATCH / MISMATCH vs SV]
  ij_updated_at   : <value>

[repeat SV/IJ block for each ocean leg]

MILESTONE PROCESSOR
  job_id          : <value>
  ETD in MP       : <value or "not found">  [MATCH / MISMATCH vs IJ]
  ATD in MP       : <value or "not found">  [MATCH / MISMATCH vs IJ]
  ETA in MP       : <value or "not found">  [MATCH / MISMATCH vs IJ]
  ATA in MP       : <value or "not found">  [MATCH / MISMATCH vs IJ]
  mp_updated_at   : <value>

───────────────────────────────────────────────────────
ROOT CAUSE
───────────────────────────────────────────────────────
  [one sentence]

CULPRIT
  [DFS validation failure — rule: <name> |
   SV pending — promote via UI: vessel_voyage_id: <id> |
   SV sync issue — IJ: <x>, SV: <y> |
   MP → IJ propagation issue — MP: <x>, IJ: <y> |
   Provider not sent to MP — check DUST/DAS |
   All layers in sync — escalate to CP]

RECOMMENDED ACTION
  [specific action]
═══════════════════════════════════════════════════════
```