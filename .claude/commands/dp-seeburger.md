# DP Child: SEEBURGER

Fetches raw Seeburger blob and runs the transformation script. Only `EQUIPMENT_EVENT` type passes; trigger/timing are passthrough enum names.

## Input

```
job_id:    <UUID>
container: <CONTAINER_NUMBER>
```

$ARGUMENTS

---

## Run

DP ID: `86dc318d-81f6-471f-b7c1-ec4edaced98c`

```bash
curl -sf "http://localhost:8085/data-provider/86dc318d-81f6-471f-b7c1-ec4edaced98c/job/<job_id>/data/<container>" \
  | python3 .claude/scripts/dp_transform_seeburger.py --job-id <job_id> --container <container>
```

If 404: blob deleted. Stop and note.

---

## What the script checks

- Reads `messages[].eventTrigger.name` / `.eventTiming.name` / `.eventType.name`
- Drops any message where `eventType != EQUIPMENT_EVENT`
- Validates `eventTrigger.name` against Milestone enum; drops unknown values
- Validates `eventTiming.name` against EventTiming enum; drops unknown values
- Applies deduplication: unique by (trigger, timing, locationCode)
- Shows vessel references per message
