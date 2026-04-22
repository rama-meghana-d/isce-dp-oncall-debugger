# Triage Ticket

Reads a Jira ticket (description + last 5 comments), understands the problem statement, and either kicks off the full investigation chain (`/incident-root-cause-investigator` → sub-skill) or posts a "no proper info" comment and reassigns to the current assignee.

## Input

```
ticket: <JIRA_KEY_OR_URL>    e.g. ISCE-14280
```

$ARGUMENTS

---

## Step 1 — Fetch Ticket

Load `mcp__claude_ai_Atlassian__getJiraIssue` via ToolSearch.

Use `cloudId: maersk-tools.atlassian.net` and `responseContentFormat: markdown`.

Extract from the response:
- `summary` — one-line ticket title
- `description` — full problem description
- `assignee` — current assignee account ID and display name (this is the person to reassign back to if clarification is needed)
- `reporter` — who filed the ticket
- `status` — ticket status

---

## Step 2 — Fetch Last 5 Comments

Load `mcp__claude_ai_Atlassian__fetch` via ToolSearch.

Fetch the last 5 comments on the ticket, ordered newest-first:
```
GET https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3/issue/<ISSUE_KEY>/comment?maxResults=5&orderBy=-created
```

For each comment note: author, timestamp, and full body text.

**Context-building rules:**
- Treat the description as the baseline problem statement.
- Comments may **refine, correct, or add to** the description — always prefer the most recent information when there is a conflict.
- If a comment contains a resolution or "no platform fault" conclusion from a previous investigation, note it — do not re-investigate.
- If a comment asks a clarifying question that is still unanswered, treat those fields as MISSING even if the description has a value.

---

## Step 3 — Extract Fields

From the description **and** the 5 most recent comments (combined), extract:

| Field | Description | Status |
|-------|-------------|--------|
| `container` | Container number (e.g. CSLU6273978) | FOUND / MISSING |
| `expected` | What should have happened | FOUND / MISSING |
| `actual` | What is currently showing / happening | FOUND / MISSING |
| `subscription_id` | If mentioned anywhere | FOUND / MISSING |
| `journey_id` | If mentioned anywhere | FOUND / MISSING |
| `booking_ref` | BL or booking reference | FOUND / MISSING |

---

## Step 4 — Understand the Problem Statement

After extracting fields, answer this question: **"Do I understand what went wrong well enough to investigate?"**

This is a holistic judgement, not a mechanical checklist. You understand the problem if you can answer all three:
1. **What object is affected?** (container number, or at minimum a booking/BL to resolve one)
2. **What is the discrepancy?** (expected state vs actual state — even if partially inferred from context)
3. **What category of fault does this look like?**

| Keywords | Category |
|----------|----------|
| "port of discharge", "port of loading", "POD", "POL", "wrong port", "incorrect port", "legs", "route", "transport plan" | `TRANSPORT_PLAN` |
| "vessel name", "voyage number", "wrong vessel", "wrong voyage" | `VESSEL_VOYAGE` |
| "ETA", "ETD", "ATA", "ATD", "arrival date", "departure date", "timestamp", "date wrong" | `TIMESTAMP` |
| "milestone", "missing event", "event not showing", "no history", "audit trail", "event missing" | `MILESTONE` |
| "duplicate", "two journeys", "merged", "split shipment", "stitching" | `STITCHING` |
| Cannot determine | `UNCLEAR` |

**You understand the problem if:**
- A container (or BL/booking to resolve one) is identifiable, AND
- The discrepancy between expected and actual can be stated clearly, AND
- The category is anything other than `UNCLEAR`

**You do NOT understand the problem if:**
- No container or BL number is anywhere in the ticket
- The description is too vague to state what is wrong (e.g., "tracking not working", "please check")
- Category is `UNCLEAR` even after reading all comments

---

## Step 5 — Route or Escalate

### If UNDERSTOOD → Invoke the full investigation chain

Invoke `/incident-root-cause-investigator` with all resolved identifiers. This skill will:
- Phase A: bind journey_id from SIR if not already known
- Phase B: verify DOS output layer
- Phase C: verify DFS guardrail layer
- Phase D: issue verdict (platform fault or no fault)
- Phase E: classify and automatically invoke the appropriate sub-skill:
  - `/transport-plan-investigation` for transport plan / legs issues
  - `/vessel-event-investigation` for vessel name / voyage / timestamp issues
  - Milestone investigation (DOS → GIS → MCE → MP → DUST chain) for milestone issues
  - Stitching investigation for duplicate journey / merge issues

Pass all resolved identifiers:

```
/incident-root-cause-investigator
  container: <CONTAINER>
  journey_id: <JOURNEY_ID if found, else omit>
  subscription_id: <SUBSCRIPTION_ID if found, else omit>
  expected: <EXPECTED>
  actual: <ACTUAL>
```

State the category and why you understood the problem statement.

---

### If NOT UNDERSTOOD → Post "no proper info" comment and reassign to assignee

Load `mcp__claude_ai_Atlassian__addCommentToJiraIssue` via ToolSearch.

Post a comment on the ticket:

```
Hi [reporter name or "team"],

This ticket does not contain enough information for a data platform investigation. Specifically:

**What is missing:**
<List each gap — e.g.>
- No container number found — required to trace the journey through SIR → DOS → GIS.
- No description of expected vs actual behavior — we need to know what should be showing and what is actually showing.
- Issue category unclear — please describe whether this is about transport plan legs, vessel name/voyage, event timestamps, missing milestones, or duplicate journeys.

**To reopen investigation, please provide:**
- Container number (e.g. MSCU1234567) or BL / booking reference
- What exactly should be showing (expected)
- What is actually showing (actual)
- Approximate date the issue was first noticed
- Subscription ID or Journey ID if available from your system

Please update the ticket with the above details.

Thanks,
ISCE Data Platform On-Call
```

Then load `mcp__claude_ai_Atlassian__editJiraIssue` via ToolSearch and **reassign the ticket back to the current assignee** (the `assignee` account ID captured in Step 1) so it lands back in their queue for clarification.

---

## Step 6 — Output Summary

Always print a triage summary:

```
## Triage Summary — <TICKET_KEY>

**Title:** <summary>
**Status:** <ticket status>
**Category:** <TRANSPORT_PLAN | VESSEL_VOYAGE | TIMESTAMP | MILESTONE | STITCHING | UNCLEAR>

### Extracted Fields
| Field         | Value              | Status          |
|---------------|--------------------|-----------------|
| container     | <value or —>       | FOUND / MISSING |
| expected      | <value or —>       | FOUND / MISSING |
| actual        | <value or —>       | FOUND / MISSING |
| journey_id    | <value or —>       | FOUND / MISSING |
| subscription_id | <value or —>     | FOUND / MISSING |

### Understanding Check
<One sentence: why I understood / why I did NOT understand the problem>

### Decision
<UNDERSTOOD → invoking /incident-root-cause-investigator → will continue to sub-skill>
  OR
<NOT UNDERSTOOD → "no proper info" comment posted, ticket reassigned to <assignee name>>
```

---

## Handling Edge Cases

- **Ticket is not a data platform issue** (UI bug, CP configuration, SCP booking error with no ISCE involvement): Post a comment naming the correct team (CP / SCP / UI), then reassign to assignee.
- **Ticket already has a resolution comment** from a prior investigation: Summarise the existing finding — do not re-investigate.
- **Multiple containers mentioned**: Extract all, flag that each must be investigated separately, ask reporter to confirm the primary container if ambiguous.
- **Ticket status is already Closed/Done**: Warn the user before taking any action — do not post comments on closed tickets unless explicitly confirmed.
