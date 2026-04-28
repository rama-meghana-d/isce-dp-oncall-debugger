# DP Child: GNOSIS

Fetches raw Gnosis blob and runs the transformation script. Handles both flat-field and raw-milestone payload formats automatically.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `4677b314-13bf-47ad-a859-b65165839617`

```bash
curl -sf "http://localhost:8085/data-provider/4677b314-13bf-47ad-a859-b65165839617/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_gnosis.py --job-id <job_id> --container <container>
```

If 404: blob deleted or job not published. Stop and note.

---

## What the script checks

- Auto-detects format: flat milestone datetime fields vs raw milestones list
- Maps flat fields (`in_gate_dt`, `vessel_ata_dt`, etc.) → `eventTrigger`
- Maps milestone `standard_event_desc` → `eventTrigger`
- Maps flag `A/E` → `ACTUAL/ESTIMATED`
- Drops `NOTIFY_DT` events and events with null timestamps
- Shows transport plan legs if present
