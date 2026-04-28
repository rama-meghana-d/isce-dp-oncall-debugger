# DP Child: VIZION

Fetches raw Vizion blob and runs the transformation script to produce a side-by-side raw↔transformed table.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `4677b314-13bf-47ad-a859-b65165839616`

```bash
curl -sf "http://localhost:8085/data-provider/4677b314-13bf-47ad-a859-b65165839616/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_vizion.py --job-id <job_id> --container <container>
```

If `curl` returns 404: blob deleted or job has not published data. Stop and note.

---

## What the script checks

- Maps raw `type` × `transport_mode` codes → `eventTrigger`
- Maps `event_classifier` (ACT/EST/PLN) → `eventTiming`
- Applies description-based overrides (e.g. "Gate in empty return" → `EMPTY_CONTAINER_RETURNED`)
- Flags AIS-source events as DROPPED (`vizion-exclude-ais-events=true`)
- Shows transport plan legs if present
- Prints counts and root cause guide
