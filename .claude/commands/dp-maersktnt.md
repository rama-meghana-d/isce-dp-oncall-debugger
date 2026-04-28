# DP Child: MAERSKTNT

Fetches raw MaerskTnT blob and runs the transformation script.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `ec65ad85-ddde-438a-b953-eae97d5ca376`

```bash
curl -sf "http://localhost:8085/data-provider/ec65ad85-ddde-438a-b953-eae97d5ca376/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_maersktnt.py --job-id <job_id> --container <container>
```

If 404: blob deleted or no data published. Stop and note.

---

## What the script checks

- Walks `containers[].locations[].events[]` structure
- Maps `eventTriggerName` → `eventTrigger` (including `isEmpty` branching for GATE-IN/OUT)
- Maps `eventTimingType` (ACTUAL/ESTIMATED/PLANNED/EXPECTED→ESTIMATED) → `eventTiming`
- Drops events with null `eventDatetime`
- Drops events with unknown trigger/timing codes
- Shows transport plan legs if present
