# ISCE On-Call Skills — Usage Guide

Quick reference for all available slash commands. Start every incident with `/incident-root-cause-investigator` — it will route to the right sub-skill automatically.

---

## `/incident-root-cause-investigator`

**What it does**
Master triage entry point. Verifies the DOS and DFS layers to determine whether the data platform is at fault, then classifies the issue and invokes the appropriate sub-skill with all identifiers already resolved.

**Phases:**
1. Binds identifiers (resolves `journey_id` from SIR if not provided)
2. Checks DOS `intelligent_journey` for correct output 
3. Checks DFS guardrail layer via GET /guardRail/journeyId/{id}/unitOfTracking/{container} — reads validation_passed_result (passed, message published) or validation_result (failed, message dropped +   
   ▎ reason)
4. Delivers verdict: platform fault vs. CP/SCP/provider fault
5. Routes to the right sub-skill

**Issues it can debug**
- "I don't know where to start — something is wrong with this container"
- "Customer says the journey data is wrong — is it us or the consumer?"
- Any incident where the root layer is not yet known
- Incidents that could span transport plan, vessel events, or milestones

**Usage examples**

```
/incident-root-cause-investigator
  container: MSCU1234567
  journey_id: 9a24d3ad-baf0-41f4-b6d2-e49ad6d95acd
  expected: container should show 3 ocean legs with correct ETD/ATD at CNSHA
  actual: only 2 legs shown and ETD at CNSHA is missing
```

```
/incident-root-cause-investigator
  container: TCKU3456789
  subscription_id: sub-abc-123
  expected: ATD at SGSIN should be 2026-03-10
  actual: ATD showing as 2026-03-12 in consumer platform
```

```
/incident-root-cause-investigator
  container: FFAU4796305
  journey_id: 9a24d3ad-baf0-41f4-b6d2-e49ad6d95acd
  expected: vessel name should be SEASPAN SANTOS on leg 1
  actual: vessel name showing as EVER GIVEN
```

---

## `/transport-plan-investigation`

**What it does**
Decision-tree RCA for transport plan issues. Traces the transport plan through:
- TPP `journey` table (booking-level and execution-level rows)
- `journey_updates` (raw DP transport plan updates)
- `journey_reconciliation_updates` (Route Reconciliation output)
- DOS `intelligent_journey` transport legs

Pinpoints whether the wrong plan came from the data provider, CP/SCP, or a Route Reconciliation merge bug.

**Issues it can debug**
- Wrong number of legs (extra leg, missing leg)
- Wrong port in a leg (e.g., CNSHA instead of CNNGB as transhipment)
- Wrong vessel or voyage on a transport plan leg
- Leg mode incorrect (OCEAN shown as RAIL)
- Transport plan not updating after booking amendment
- Route Reconciliation wrote incorrect leg data
- DP sent a plan that overwrote a correct CP plan

**Usage examples**

```
/transport-plan-investigation
  journey_id: 9a24d3ad-baf0-41f4-b6d2-e49ad6d95acd
  container: MSCU1234567
```

```
/transport-plan-investigation
  journey_id: 3f8a1c22-4d9e-41b2-bc88-1a2b3c4d5e6f
  container: TCKU3456789
  job_id: job-789xyz
```

**Typical findings**
| Symptom | Likely culprit found |
|---|---|
| Extra leg in IJ not in CP booking | DP sent extra leg in `journey_updates` |
| Missing transhipment port | Route Reconciliation dropped a leg in `journey_reconciliation_updates` |
| Plan reverted to old route | CP re-sent old booking-level row via `cp-sjs-data` topic |
| Wrong vessel on leg | DP update in `journey_updates` has wrong vessel; or Route Reconciliation did not apply vessel from SV |

---

## `/vessel-event-investigation`

**What it does**
RCA for a specific vessel event on a specific ocean leg being wrong — either the vessel name / voyage number is incorrect, or the ETA / ATA / ETD / ATD timestamp is wrong. Traces:
- Intelligent Journey `eventConnections` (vessel/voyage) and `timestamp`
- Shipment Visibility port call timestamps (`vessel_voyage_port_call_timestamp`)
- Milestone Processor `enriched_data` (if IJ is wrong)
- DUST `milestonepipeline` raw payload (if MP is wrong)
- DAS `das_request_store` to identify which data provider sent bad data

**Issues it can debug**
- "ETA at SGSIN is showing 2026-03-15 but should be 2026-03-10"
- "ATD at PKBQM is missing / wrong"
- "Vessel name showing EVER GIVEN but should be SEASPAN SANTOS on leg 2"
- "Voyage number is 502W but booking says 503E"
- "ATA event has timestamp from 2 days ago — wrong date"

**Two investigation modes**

| `problem` value | What it checks |
|---|---|
| `vessel_voyage` | Traces IJ → MP → DUST to find where the wrong vessel name or voyage number entered |
| `timestamp` | Starts at SV port call timestamp, checks pending updates, then traces back to IJ → MP if SV is wrong |

**Usage examples**

```
/vessel-event-investigation
  journey_id: 9a24d3ad-baf0-41f4-b6d2-e49ad6d95acd
  container: FFAU4796305
  leg_sequence: 1
  event_type: ATD
  location_code: PKBQM
  problem: timestamp
  vessel_name: SEASPAN SANTOS
  voyage_number: 604S
  expected_date: 2026-01-27
```

```
/vessel-event-investigation
  journey_id: 3f8a1c22-4d9e-41b2-bc88-1a2b3c4d5e6f
  container: MSCU1234567
  leg_sequence: 2
  event_type: ETA
  location_code: DEHAM
  problem: timestamp
  vessel_name: MSC ANNA
  voyage_number: AW312R
  expected_date: 2026-04-05
```

```
/vessel-event-investigation
  journey_id: 7b3e9f1a-0c2d-4e5f-a678-b90c1d2e3f4a
  container: TCKU3456789
  leg_sequence: 1
  event_type: ETD
  location_code: CNSHA
  problem: vessel_voyage
  expected_vessel_name: EVER ACE
  expected_voyage_number: 0123W
```

**Decision path summary**

```
problem: vessel_voyage
  IJ wrong → MP wrong → DUST wrong → Data provider sent bad vessel/voyage
  IJ wrong → MP wrong → DUST correct → MP enrichment bug
  IJ correct → SV wrong → SV event_connection mismatch (DI/SV bug)

problem: timestamp
  SV correct (date matches) → DFS guardrail check → CP/consumer layer issue
  SV wrong, pending_timestamp correct → Manual port call promotion needed in SV
  SV wrong, no pending → IJ wrong → MP wrong → Data provider sent wrong timestamp
```

---

## `/milestone-timestamp-audit`

**What it does**
Audits all actual (and/or estimated) milestone events for one or more containers. For each event shows:
- **Event timestamp** — when the physical event occurred (as reported by the provider)
- **Added to system** — when the platform processed and stored it (GIS `created_on`)

Also checks VRDAN for raw payload coverage per container (how many payload versions received and when).

Useful for SLA investigations, bulk incident triage, and "when did we first know about this event?" questions.

**Issues it can debug**
- "Why is the ATA showing so late — did the provider send it late or did we process it late?"
- Bulk audit across 10–20 containers to find which ones have missing actuals
- "Container has GATE_IN but no LOADED_ON_VESSEL — is the event in the pipeline?"
- Checking whether a provider backfilled events (large gap between event_timestamp and added_to_system)
- Confirming VRDAN raw payload coverage (how many data versions a provider sent)
- "Did we ever receive an ATD for this container?"

**Usage examples**

```
/milestone-timestamp-audit
  containers: FFAU4796305
  event_timing: ACTUAL
```

```
/milestone-timestamp-audit
  containers: MSCU1234567, TCKU3456789, OOLU8765432
  event_timing: ACTUAL
  event_trigger: VESSEL_DEPARTURE
```

```
/milestone-timestamp-audit
  containers: MSCU1234567, TCKU3456789, OOLU8765432, CMAU5544332, HLXU9988776
  event_timing: ALL
```

**Output tables**

| Table | What it shows |
|---|---|
| Actual Events per Container | Every actual milestone — event timestamp vs. platform ingestion time |
| VRDAN Raw Payload Coverage | Per container: provider name, total payload versions received, first/last received |

**Anomaly flags automatically checked**
- Container with no SIR subscription (cannot be tracked)
- Container with no actual events at all
- Missing `VESSEL_DEPARTURE` between `LOADED_ON_VESSEL` and `UNLOADED_FROM_VESSEL` (possible GIS rejection)
- Large backfill delay (`added_to_system` >> `event_timestamp` by hours or days)
- Containers sharing a journey_id (BL-level subscription)
- No VRDAN coverage (data arrives via Hook Service, not DAS pull — raw payload not archived)

> **VRDAN retention:** new event version = 1 month · old event version = 15 days.
> If querying events older than these windows, VRDAN rows may be absent even if data was received.

---

## Quick Skill Chooser

| Situation | Use |
|---|---|
| Don't know where to start | `/incident-root-cause-investigator` |
| Legs wrong / missing / extra port / wrong route | `/transport-plan-investigation` |
| Vessel name or voyage number wrong on a leg | `/vessel-event-investigation` with `problem: vessel_voyage` |
| ETA / ATA / ETD / ATD date is wrong | `/vessel-event-investigation` with `problem: timestamp` |
| Bulk audit — when did events arrive, which containers are missing actuals | `/milestone-timestamp-audit` |
