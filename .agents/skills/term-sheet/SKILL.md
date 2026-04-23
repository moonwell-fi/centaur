---
name: term-sheet
description: "Generates Paradigm form term sheets for venture investments. Use when asked to draft a term sheet, create a term sheet, generate term sheet, or prepare term sheet for a deal."
---

# Term Sheet Generator

Generates a Paradigm-standard term sheet as a Word document (.docx) for a new venture investment. Assumes Paradigm is the lead investor.

## When To Use

Use when the user asks to:
- "draft a term sheet for X"
- "generate a term sheet for [Company] Series [X]"
- "create a term sheet — $10M at $100M post"
- "prepare term sheet for [deal]"

## Required Deal Inputs (DRI)

Gather these from the user before generating. If the user provides them upfront, don't re-ask:

| Input | Example | Notes |
|-------|---------|-------|
| **Company name** | Acme Corp | Legal entity name |
| **Series** | A, B, C, etc. | Round designation |
| **Investment amount** | $10,000,000 | Paradigm's check size (total aggregate proceeds) |
| **Post-money valuation** | $100,000,000 | Always post-money |
| **Option pool** | 10% | Percentage of post-money cap |
| **Board seat** | Yes / No | Whether Paradigm gets a board seat |
| **Observer seat** | Yes / No | Whether Paradigm gets a board observer |
| **Crypto company** | Yes / No | If No, all token provisions are removed |

## Optional Inputs (have defaults)

| Input | Default | Notes |
|-------|---------|-------|
| **No-shop period** | 30 days | Exclusivity window |
| **Counsel fee cap** | $75,000 | Paradigm counsel expense cap |
| **Qualified IPO threshold** | $100,000,000 | Gross proceeds threshold |
| **Founder vesting — % vested at closing** | 25% | What's already vested |
| **Founder vesting — remaining years** | 4 | Total vesting schedule |
| **Founder vesting — cliff months** | 12 | Cliff period |
| **Equity incentive plan shares** | (left blank) | Shares reserved for option pool |
| **Automatic conversion consent %** | 60% | Majority threshold |
| **Protective provisions consent %** | 50% | Majority threshold |

## Paradigm Standard Positions (Auto-Applied)

These are **not** asked — they are hardcoded as Paradigm's form:

- **Dividends**: Non-cumulative, as-converted basis only (no fixed dividend rate)
- **Liquidation preference**: 1x non-participating preferred
- **Anti-dilution**: Broad-based weighted average (BBWA)
- **Redemption**: None
- **Drag-along**: Majority of preferred + majority of key holders
- **ROFR**: Company first, then investors; 30-day window
- **Founder carveout**: 2% without consent
- **Registration rights demand**: 5 years after closing or 180 days after IPO
- **Pro rata rights**: Full pro rata on future issuances

If the user requests a deviation from any of these (e.g., participating preferred, full ratchet), generate the doc as requested but **warn them** that it deviates from Paradigm standard.

## Steps

### Step 1: Gather Inputs

Ask the user for the required DRI inputs above. Accept them in any format — a single message with all details, a deal memo, or conversationally. Parse what's provided and only ask for what's missing.

### Step 2: Confirm Parameters

Before generating, show a summary table of all inputs (DRI + defaults) and ask the user to confirm or adjust:

```
## Term Sheet Parameters
| Parameter | Value |
|-----------|-------|
| Company | Acme Corp |
| Series | A |
| Investment Amount | $10,000,000 |
| Post-Money Valuation | $100,000,000 |
| Option Pool | 10% |
| Board Seat | Yes |
| Observer Seat | Yes |
| Crypto Company | No |
| No-Shop Period | 30 days |
| Counsel Fee Cap | $75,000 |
| ... | ... |

Confirm or adjust?
```

### Step 3: Generate the Document

Run the generation script with the confirmed parameters:

```bash
python3 scripts/generate.py '<JSON parameters>'
```

The JSON parameter object:
```json
{
  "company_name": "Acme Corp",
  "series": "A",
  "investment_amount": 10000000,
  "post_money_valuation": 100000000,
  "option_pool_percent": 10,
  "board_seat": true,
  "observer_seat": true,
  "is_crypto": false,
  "no_shop_days": 30,
  "counsel_fee_cap": 75000,
  "qualified_ipo_threshold": 100000000,
  "founder_vesting_percent": 25,
  "founder_vesting_years": 4,
  "founder_cliff_months": 12,
  "auto_conversion_percent": 60,
  "protective_provisions_percent": 50,
  "equity_plan_shares": null
}
```

The script outputs the path to the generated `.docx` file.

### Step 4: Upload the .docx to Slack

⚠️ **CRITICAL — Do not skip this step.** The user must receive the actual .docx file in Slack. Do NOT just print the file path.

**Step 4a: Get the Slack channel**

The channel name or ID comes from the thread metadata (the `metadata` field in the user message, e.g. `metadata.channel`). If you don't have it, check the environment or ask the user. Common channel names: `deals`, `legal`, or the DM channel.

**Step 4b: Upload the file using the slack tool**

```bash
curl -s -X POST http://api:8000/tools/slack/upload_file \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $CENTAUR_API_KEY" \
  -d '{
    "channel": "<channel_name_or_id>",
    "file_path": "<absolute path to generated .docx>",
    "filename": "Term Sheet - <Company> Series <X>.docx",
    "title": "Term Sheet - <Company> Series <X>",
    "comment": "Here is the generated term sheet."
  }'
```

If the request came from a Slack thread, include `"thread_ts": "<thread_ts>"` to upload in the thread.

### Step 5: Deliver

Confirm the file was uploaded and the user can download it. Offer:
- "Want me to review this term sheet against the Paradigm playbook?"
- "Want me to adjust any terms?"
- "Want me to generate a version with different economics?"

## Review Mode

If the user provides an **incoming** term sheet (from a counterparty) and asks to review it, use the `reviewing-financing-documents` skill instead — it handles full redline review. This skill is for **generation**.

However, if the user asks to "review" a term sheet that this skill just generated, re-read the output file and verify all terms match the confirmed parameters.

## Output Rules

- Always output a `.docx` file — never markdown-only
- File name format: `Term Sheet - [Company] Series [X].docx`
- Always upload the .docx directly to the Slack thread so the user can download it
- If Slack upload fails, tell the user and offer to retry
