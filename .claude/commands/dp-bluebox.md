# DP Child: BLUEBOX

Fetches raw Bluebox blob and runs the transformation script. Air mode only (MAWB/HAWB).

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>   (may be MAWB or container number)
```

$ARGUMENTS

---

## Run

DP ID: `4677b314-13bf-47ad-a859-b65165839620`

```bash
curl -sf "http://localhost:8085/data-provider/4677b314-13bf-47ad-a859-b65165839620/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_bluebox.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Checks for null `mawbInfo` — if null, entire payload is dropped and script stops
- Checks for empty `shipments` list — dropped if empty
- Validates `granularity` (HAWB/tracking unit) per shipment; drops if invalid
- Maps raw movement `status` codes (DEP/ARR/DLV/RCS/PUP) → `eventTrigger`
- Shows MAWB flight segments for routing context
- Notes: Bluebox is air mode only — irrelevant for ocean containers
