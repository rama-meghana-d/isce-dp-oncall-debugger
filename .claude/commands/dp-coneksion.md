# DP Child: CONEKSION

Fetches raw Coneksion blob and runs the transformation script. Critical: `UNMAPPED_EVENT` in any event drops the **entire shipment**.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `4677b314-13bf-47ad-a859-b65165839625`

```bash
curl -sf "http://localhost:8085/data-provider/4677b314-13bf-47ad-a859-b65165839625/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_coneksion.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Scans each shipment for `UNMAPPED_EVENT` — if found, marks all events in that shipment as dropped
- Validates remaining `eventTrigger` values against Milestone enum
- Passthrough for valid triggers (Coneksion sends pre-transformed events)
- Shows which shipments were dropped and why
