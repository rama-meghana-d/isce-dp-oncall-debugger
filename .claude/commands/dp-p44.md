# DP Child: P44 (P44_NEW / P44_ROAD_NEW)

Fetches raw P44 blob and runs the transformation script.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
dp_name:   P44_NEW | P44_ROAD_NEW
```

$ARGUMENTS

---

## Run

Select DP ID based on `dp_name`:
- `P44_NEW` → `4677b314-13bf-47ad-a859-b65165839623`
- `P44_ROAD_NEW` → `dfffaac1-770c-4434-b5ca-14721dd5a9f2`

```bash
DP_ID="4677b314-13bf-47ad-a859-b65165839623"   # change for P44_ROAD_NEW
curl -sf "http://localhost:8085/data-provider/${DP_ID}/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_p44.py --job-id <job_id> --container <container> --dp-name <dp_name>
```

If 404: blob deleted or no data published. Stop and note.

---

## What the script checks

- Detects and drops master shipment records upfront
- Maps raw `eventType` → `eventTrigger` (DCSA-style passthrough + key remaps)
- Picks timing from `actualDateTime` / `estimatedDateTime` / `plannedDateTime` fields
- Drops geofence events (`source=GEOFENCE`, flag default=true)
- Drops events with no timing data
- Shows transport plan legs if present
