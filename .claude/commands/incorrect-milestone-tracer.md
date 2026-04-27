# Incorrect Milestone Tracer

Traces a single incorrectly-typed milestone event (wrong `eventTrigger` or `eventTiming`) layer by layer through the ISCE pipeline — IJ → MCE → MP → DUST — and identifies whether the root cause is a DAS transformation bug or a data provider data quality issue.

## Input

```
container:         <CONTAINER_NUMBER>
journey_id:        <JOURNEY_ID>          (optional — resolved from SIR if omitted)
incorrect_trigger: <EVENT_TRIGGER>       e.g. VESSEL_ARRIVAL
event_timing:      <EVENT_TIMING>        e.g. ESTIMATED
location_code:     <UNLOC>               e.g. NOGEN
```

$ARGUMENTS

---

## Python Extraction Script (PARSE_SCRIPT)

Define this script once. Reuse it at Steps 2–5 by substituting `<FILE>`, `<TRIGGER>`, `<TIMING>`, `<LOCODE>`.

```python
python3 -c "
import json,sys
data=json.load(open(sys.argv[1]))
text=data[0]['text'] if isinstance(data,list) else json.dumps(data)
obj=json.loads(text) if isinstance(text,str) else text
trigger,timing,locode=sys.argv[2],sys.argv[3],sys.argv[4]
rows=obj.get('rows') or (obj if isinstance(obj,list) else [obj])
found=[]
for row in rows:
    journey=row.get('journey') or {}
    if isinstance(journey,str): journey=json.loads(journey)
    for i,leg in enumerate(journey.get('shipmentJourneyLegs',[])):
        for ev in leg.get('events',[]):
            if ev.get('eventTrigger')==trigger and ev.get('eventTiming')==timing:
                for loc in ev.get('locations',[]):
                    if locode in json.dumps(loc):
                        refs=[r.get('reference') for r in ev.get('references',[]) if any(k in r.get('referenceTypeEnum','') for k in ('VESSEL','VOYAGE','TRANSPORT'))]
                        found.append({'leg':i+1,'trigger':trigger,'timing':timing,'ts':ev.get('timestamp'),'refs':refs})
print(json.dumps(found,indent=2) if found else 'NOT_FOUND')
" <FILE> <TRIGGER> <TIMING> <LOCODE>
```

---

## Step 1 — Resolve journey_id (skip if already provided)

Load `mcp__sir-db__query` via ToolSearch.

```sql
SELECT journey_id, status, created_on
FROM subscription
WHERE unit_of_tracking::text LIKE '%<CONTAINER>%'
ORDER BY created_on DESC LIMIT 1;
```

Record `journey_id` and `status`. If `TRACKING_STOPPED`, carry that as context.

---

## Step 2 — Check DOS Intelligent Journey

Load `mcp__dos-db__query` via ToolSearch.

```sql
SELECT journey_id, journey::text, updated_at
FROM intelligent_journey
WHERE journey_id = '<JOURNEY_ID>'
ORDER BY updated_at DESC LIMIT 1;
```

The result JSON is large — auto-save to a file (the MCP tool does this automatically when the response is large). Run PARSE_SCRIPT on the saved file:

```bash
python3 -c "..." /path/to/saved_file.json <INCORRECT_TRIGGER> <EVENT_TIMING> <LOCATION_CODE>
```

**FOUND** → record `leg`, `timestamp`, `refs` → continue to Step 3.

**NOT_FOUND** → the incorrect event does not exist in IJ at all. Stop. Declare: **event was never written to IJ** — investigate MCE/GIS rejection upstream. Check `gis-db: audit_trail` WHERE `transactionType = REJECTED` AND `journey_id = '<JOURNEY_ID>'`.

---

## Step 3 — Check MCE Triangulation Audit

Load `mcp__mce-db__query` via ToolSearch.

```sql
SELECT id, journey_id, event_trigger, event_timing, location_code,
       data_provider, applied_rules::text, created_at
FROM isce_triangulation_audit
WHERE journey_id = '<JOURNEY_ID>'
  AND event_trigger = '<INCORRECT_TRIGGER>'
  AND location_code = '<LOCATION_CODE>'
ORDER BY created_at DESC LIMIT 5;
```

**Result rows exist** → record `data_provider`, `applied_rules` → continue to Step 4.

**No rows** → event never reached MCE (e.g. GIS rejected it). Stop. Declare: **GIS rejection** — check `gis-db: audit_trail` WHERE `transactionType = REJECTED` AND `location_code = '<LOCATION_CODE>'` AND `journey_id = '<JOURNEY_ID>'`.

---

## Step 4 — Check Milestone Processor

Load `mcp__milestone-processor-db__query` via ToolSearch. First confirm column names:

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'milestone'
ORDER BY ordinal_position;
```

Then query using the confirmed column names:

```sql
SELECT *
FROM milestone
WHERE journey_id = '<JOURNEY_ID>'
  AND event_trigger = '<INCORRECT_TRIGGER>'
  AND location_code = '<LOCATION_CODE>'
ORDER BY created_at DESC LIMIT 5;
```

**Found, same trigger** → same wrong event exists in MP → continue to Step 5.

**Found, different trigger** → MCE triangulation changed the event type on the way out. Record the MP value. Declare: **MCE triangulation bug** — the `applied_rules` from Step 3 explain why the type was changed.

**Not found** → event was not published by MP for this trigger+location combo. Declare: **MP enrichment gap** — check `subscription_milestone_metadata` for this `journey_id`.

---

## Step 5 — Check DUST Milestonepipeline

Load `mcp__dust-db__query` via ToolSearch. First confirm column names:

```sql
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'milestonepipeline'
ORDER BY ordinal_position;
```

Then query. The `pipeline_id` pattern is `{job_id}_{container}`:

```sql
SELECT pipeline_id, data_provider, created_at,
       LEFT(data::text, 300) AS data_preview
FROM milestonepipeline
WHERE pipeline_id LIKE '%<CONTAINER>%'
ORDER BY created_at DESC LIMIT 10;
```

From the results:
- Extract `pipeline_id` → split on `_<CONTAINER>` to recover `job_id`.
- Note `data_provider`.

Check whether the incorrect trigger is present in `data_preview`. If the preview is truncated, save the full result to a file and run PARSE_SCRIPT adapted for the DUST row structure (the `data` column holds the milestone event JSON):

```python
python3 -c "
import json,sys
rows=json.load(open(sys.argv[1]))
trigger,timing,locode=sys.argv[2],sys.argv[3],sys.argv[4]
found=[]
for row in (rows if isinstance(rows,list) else [rows]):
    data=row.get('data') or {}
    if isinstance(data,str): data=json.loads(data)
    ev=data if 'eventTrigger' in data else {}
    if ev.get('eventTrigger')==trigger and ev.get('eventTiming')==timing:
        locs=json.dumps(ev.get('locations',[]))
        if locode in locs:
            found.append({'pipeline_id':row.get('pipeline_id'),'provider':row.get('data_provider'),'ts':ev.get('timestamp')})
print(json.dumps(found,indent=2) if found else 'NOT_FOUND')
" <FILE> <TRIGGER> <TIMING> <LOCODE>
```

**Incorrect trigger present in DUST** → DUST published the wrong event type. The error is in the **DAS transformation layer** for the named `data_provider`. Proceed to Step 6.

**Correct trigger in DUST** → DUST output was correct; the wrong trigger was introduced by MP or MCE (a processing layer bug). Report the divergence layer and stop.

---

## Step 6 — Transformation Code RCA

Report confirmed identifiers:

```
job_id:        <JOB_ID>
data_provider: <PROVIDER>
pipeline_id:   <PIPELINE_ID>
```

### Step 6a — Fetch raw payload from VRDAN (in-session)

Load `mcp__vrdan-db__query` via ToolSearch.

```sql
SELECT raw_payload::text, data_provider, received_at
FROM versioned_payload_data
WHERE job_id = '<JOB_ID>'
ORDER BY received_at DESC LIMIT 1;
```

- **Row returned** → use `raw_payload` directly. Do NOT ask the user. Proceed to Step 6b.
- **No row** → VRDAN has no record (Hook Service tracked or payload expired). Ask the user in this session:
  > "VRDAN has no record for job_id `<JOB_ID>`. Please paste the raw payload from `<PROVIDER>` directly here."
  Once the user pastes it, proceed to Step 6b.

### Step 6b — Read the DAS transformer for the provider

Locate and read the transformer file:

| Provider | Transformer location |
|---|---|
| GNOSIS | `isce-data-sourcing/das-jobs/src/main/java/.../transform/output/gnosis/RawMilestones.java` |
| P44_NEW | `isce-data-sourcing/das-jobs/src/main/java/.../transform/output/p44/` |
| VIZION | `isce-data-sourcing/das-jobs/src/main/java/.../transform/output/vizion/` |
| MAERSKTNT | `isce-data-sourcing/das-jobs/src/main/java/.../transform/output/maersktnt/` |
| PORTCAST | `isce-data-sourcing/das-jobs/src/main/java/.../transform/output/portcast/` |
| Others | Search `das-jobs/src/main/java/.../transform/output/<provider>/` |

Find the field in the raw payload that drives the `event_trigger` mapping. Identify the mapping rule and any conditions (or absence of conditions) on location type.

### Step 6c — Two-path verdict

**Path A — Provider data quality issue**

Declare this when **any** of the following is true in the raw payload:
- The event explicitly includes vessel-type references at the inland location: `vessel_name`, `vessel_imo`, or `voyage_number` alongside the event at `final_dest_locode`.
- The provider's own event type/code unambiguously says "vessel" (e.g. `event_type: "VESSEL_EVENT"`, `transport_mode: "SEA"`) for a location that has no port (`hasPort: false`).
- The raw `event_desc` / `event_type` value would be correct **only** for a seaport yet the provider attached it to the final inland destination.

```
ROOT CAUSE:  Provider data quality issue
PROVIDER:    <PROVIDER>
JOB_ID:      <JOB_ID>
EVIDENCE:    Raw payload field <FIELD>=<VALUE> explicitly indicates a vessel event
             at <LOCATION_CODE> which is an inland DESTINATION (not a seaport).
ACTION:      Raise a data quality ticket with <PROVIDER>. Include job_id, the raw
             payload excerpt, and the specific field that is incorrect.
```

**Path B — DAS transformer bug**

Declare this when:
- The raw payload uses a generic or ambiguous term (e.g. `"event_desc": "Arrival in"`) that applies to any transport mode.
- The transformer maps it to `VESSEL_ARRIVAL` unconditionally via a static map with no check for whether the location is `pod_locode` (seaport) or `final_dest_locode` (inland).
- The same raw term at a seaport would correctly produce `VESSEL_ARRIVAL`, but at an inland location the correct trigger is `TRUCK_ARRIVAL`.

```
ROOT CAUSE:  DAS transformer bug
FILE:        <TRANSFORMER_FILE>
FIELD:       Raw payload field <FIELD>=<VALUE> is ambiguous (valid for any mode).
             The static map emits VESSEL_ARRIVAL without checking location type.
             At <LOCATION_CODE> (finalDestLocode / inland DESTINATION), the correct
             trigger is TRUCK_ARRIVAL.
ACTION:      Raise a fix with DI Engineering. The mapping must check whether
             location_code == finalDestLocode and emit TRUCK_ARRIVAL / TRUCK_DEPARTURE
             for inland destinations instead of VESSEL_ARRIVAL / VESSEL_DEPARTURE.
```

---

## Verdict Table

| Layer where divergence detected | Culprit | Action |
|---|---|---|
| Event not in IJ at all | GIS/MCE rejection | Check `gis-db: audit_trail WHERE transactionType='REJECTED'` |
| IJ has it but MCE had no row | GIS rejected before MCE | Check `gis-db: audit_trail` rejection reason |
| MCE has it with different trigger | MCE triangulation changed the type | MCE `applied_rules` bug — DI Engineering |
| MP has it with different trigger | MP enrichment changed the type | MP enrichment bug — DI Engineering |
| DUST has the wrong trigger | DAS transformer maps incorrectly | DI Engineering — fix the static mapping in the transformer |
| DUST has wrong trigger + raw data already wrong | Provider sent wrong event type | Raise data quality issue with provider |
| DUST has correct trigger | Divergence introduced by MP or MCE | Confirm which layer changed it; raise DI Engineering bug |
