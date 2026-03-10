---
name: policy-gigabrain
description: "Policy team intelligence system for Hill briefings, staffer tracking, bill analysis, vote prediction, and legislative trend detection. Use when asked about congressional meetings, staffers, legislation, regulatory actions, rulemakings, whip counts, policy team workflows, or any question about OCC, SEC, CFTC, FDIC, FinCEN, Treasury guidance, stablecoins, GENIUS Act, market structure bills, or crypto regulation. Triggers on: policy question, regulatory impact, bill analysis, legislation impact on portfolio."
---

# Policy Gigabrain

Centralized intelligence system for government affairs. Generates meeting briefers, tracks staffers and legislators, monitors legislation, predicts votes, and surfaces portfolio-relevant regulatory actions.

## ⚠️ MANDATORY FIRST STEP: Policy Explainer Index

**Before answering ANY policy or regulatory question — before running web searches or reading external sources — you MUST first check the Paradigm Policy team's internal analysis.**

```bash
call gsuite docs_read '{"doc_id":"1yiKL4NgJfT0cAXehqHvaYC1mMezKdDhxwnr8Gm8aa6c"}'
```

This is the **Policy Explainer Index** ([link](https://docs.google.com/document/d/1yiKL4NgJfT0cAXehqHvaYC1mMezKdDhxwnr8Gm8aa6c/edit?tab=t.0)), maintained by the Policy team. It contains Paradigm's internal analysis and takes on major regulatory developments (GENIUS Act, FDIC rulemakings, SEC actions, etc.).

**Why this matters:** The Policy team's analysis includes Paradigm-specific context, portfolio impact assessments, and strategic framing that external sources cannot provide. External web searches should only supplement — never replace — the team's own work.

**Workflow:**
1. Read the Policy Explainer Index to check if the topic has been analyzed
2. If an explainer exists, read the linked analysis doc for full detail
3. Only then supplement with external sources (OCC/SEC/CFTC websites, Federal Register, etc.) if needed
4. Always cite the internal analysis as the primary source

## Core Capabilities

| Function | Description |
|----------|-------------|
| **Meeting Briefers** | Auto-generate one-pagers for Hill meetings |
| **Staffer Tracking** | Track congressional/regulatory staff careers and relationships |
| **Bill Tracking** | Monitor legislation with momentum scores and status |
| **Vote Prediction** | Maintain whip sheets with stance, confidence, rationale |
| **Trend Detection** | Surface emerging patterns across jurisdictions |
| **Portfolio Impact** | Flag legislation affecting portfolio companies |

---

## Data Sources

### External APIs

| Source | Tool | Purpose |
|--------|------|---------|
| **Congress.gov** | `call congress` | Bills, members, committees, hearings, votes, amendments |
| **Federal Register** | `call fedreg` | Regulatory dockets, comment periods, rulemakings |
| **OpenFEC** | `call openfec` | Campaign contributions, candidates, committees, filings |
| **LegiStorm** | `call legistorm` | Staffer database, career tracking, office moves |
| **Plural Policy** | — | Bill momentum scores, AI summaries (future) |

### Internal Sources

| Source | Tool | Use For |
|--------|------|---------|
| **Shift / paradigmdb** | `call paradigmdb` | Portfolio companies, prior interactions, notes |
| **Slack** | `call slack search_messages` | Policy team discussions, intel |
| **GSuite** | `call gsuite` | Meeting notes, Hill interaction logs, Drive docs |
| **Semantic search** | `call search` | Archived policy documents |

---

## Workflows

### 1. Meeting Briefer Generation

Generate a one-page briefing packet before Hill meetings.

**Input:** Member/staffer name, meeting context, date

**Steps:**
1. Look up member profile via web search (committee assignments, voting history, public statements)
2. Search internal sources for prior Paradigm interactions:
   ```bash
   call slack search_messages '{"query":"from:#policy [member_name]"}'
   call gsuite gmail_search '{"query":"[member_name]"}'
   call paradigmdb notes_search '{"query":"[member_name]"}'
   ```
3. Find relevant pending legislation:
   ```bash
   call congress bills '{"congress":119}'
   call congress bill '{"congress":119,"type":"s","number":123}'
   ```
4. Check FEC for campaign contribution context:
   ```bash
   call openfec candidates '{"name":"[member_name]"}'
   call openfec contributions '{"contributor_name":"[member_name]"}'
   ```
5. Cross-reference with paradigmdb for portfolio company relevance:
   ```bash
   call paradigmdb db_query '{"query":"SELECT * FROM \"Organization\" WHERE name ILIKE '\''%relevant_company%'\'';"}'
   ```
6. Generate structured briefer (see template below)

**Briefer Template:**
```
# [MEETING TYPE]: [Member/Staffer Name]
Date: [DATE] | Location: [LOCATION]

## Profile
- **Role:** [Title, Committee assignments]
- **Party/State:** [Party - State]
- **Key Issues:** [Crypto/tech positions]
- **Prior Interactions:** [Paradigm touchpoints]

## Context
- **Meeting Purpose:** [Why we're meeting]
- **Current Priorities:** [What they care about now]

## Relevant Legislation
- [Bill Number]: [Title] - [Status, our position]

## Talking Points
1. [Point 1]
2. [Point 2]
3. [Point 3]

## Ask
[What we want from this meeting]

## Background
[Additional context, prior commitments, sensitivities]
```

### 2. Staffer Tracking

Track congressional and regulatory staffers relevant to crypto policy.

**Key Roles to Track:**
- Legislative Directors (LD)
- Committee Counsel (especially Banking, Finance, Agriculture)
- Personal Office Chiefs of Staff
- Leadership Staff
- Executive branch personnel rotating to legislative

**Data Points:**
- Current role and office
- Committee assignments
- Policy areas covered
- Career trajectory
- Paradigm touchpoints
- Alma mater (for relationship mapping)

**Flags to Surface:**
- Junior staff moving to senior roles (build relationships early)
- Committee transfers (new jurisdictional exposure)
- Executive → legislative rotations
- Departures from key offices

**Search Commands:**
```bash
# Find prior interactions with staffer
call slack search_messages '{"query":"[staffer_name]"}'
call gsuite gmail_search '{"query":"[staffer_name]"}'

# Check if mentioned in notes
call paradigmdb notes_search '{"query":"[staffer_name]"}'

# LegiStorm lookup
call legistorm staff '{"name":"[staffer_name]"}'
```

### 3. Bill Tracking & Analysis

Monitor crypto-relevant legislation across federal and state jurisdictions.

**Federal Focus:**
- Senate Banking Committee
- House Financial Services Committee
- Agriculture Committees (CFTC jurisdiction)
- Judiciary Committees (DOJ, IP)

**State Priorities:** NY, TX, CA, WY, IL (active crypto agendas)

**Track Per Bill:**
- Bill number and title
- Sponsors and cosponsors
- Committee referral and status
- Hearing schedule
- Markup dates
- Floor action timeline
- Amendment activity
- Paradigm position (support/oppose/monitor)

**Search Commands:**
```bash
# Search bills via Congress.gov API
call congress bills '{"congress":119,"limit":50}'
call congress bill '{"congress":119,"type":"hr","number":4763,"detail":"summaries"}'

# Search hearings
call congress hearings '{"congress":119,"chamber":"senate"}'
call legistorm townhalls '{}'

# Search for internal discussions
call slack search_messages '{"query":"[bill number]"}'
```

### 4. Vote Prediction / Whip Sheet

Maintain running vote counts for priority legislation.

**Whip Sheet Fields:**
| Field | Values |
|-------|--------|
| Stance | Support / Lean Support / Uncommitted / Lean Oppose / Oppose |
| Strength | Firm / Soft |
| Rationale | Why we believe this |
| Key Influencer | Who can move them |
| The Ask | What we need from them |
| Next Action | Follow-up task |
| Owner | Paradigm team member |
| Last Verified | Date of last confirmation |
| Evidence | Meeting notes, public statements |

**Prediction Inputs:**
- Historical voting patterns (VoteSmart, GovTrack)
- Cosponsor networks
- Public statements
- Committee behavior
- Party leadership signals
- Direct intelligence from meetings

**Update Process:**
1. After each Hill interaction, log stance update
2. Flag inconsistencies between public statements and private positions
3. Surface members whose stance has shifted

### 5. Coalition & Opposition Mapping

Track who is lobbying on which bills.

**Entities to Track:**
- Industry groups (Chamber, trade associations)
- Advocacy organizations
- Companies (competitors, allies)
- Think tanks
- Other crypto firms

**Per Entity:**
- Position on key bills
- Lobbying intensity (high/medium/low)
- Key contacts
- Coalition membership

### 6. Trend Detection

Surface emerging patterns before they become consensus.

**Signals to Monitor:**
- Bill introduction clusters (3+ states with similar language)
- Hearing topic frequency
- Floor statement themes
- Regulatory action patterns
- Model legislation from ALEC, ULC

**State AG Actions:**
- Enforcement actions in priority states
- Settlement patterns
- New investigation announcements

**Search Commands:**
```bash
# Search Federal Register for crypto-related regulatory actions
call fedreg search '{"query":"cryptocurrency","agency":"securities-and-exchange-commission"}'
call fedreg search '{"query":"digital assets","agency":"commodity-futures-trading-commission"}'
call fedreg search '{"query":"stablecoin","type":"PRORULE"}'

# State-level via web search
web_search "state cryptocurrency legislation 2026"
```

### 7. Regulatory Docket Monitoring

Track SEC, CFTC, FinCEN, OCC, Treasury rulemakings.

**Per Docket:**
- Agency and docket number
- Proposed rule summary
- Comment deadline
- Paradigm response status (draft/submitted/none)
- Portfolio company impact

**Search Commands:**
```bash
# Open comment periods
call fedreg comments-open '{"agency":"securities-and-exchange-commission"}'
call fedreg comments-open '{"agency":"commodity-futures-trading-commission"}'

# Search regulatory dockets
call fedreg search '{"query":"cryptocurrency","type":"RULE"}'
call fedreg search '{"query":"digital assets","type":"PRORULE","agency":"treasury-department"}'

# Get specific document details
call fedreg document '{"document_number":"2026-01234"}'
```

### 8. Portfolio Impact Flagging

Automatically surface legislation affecting portfolio companies.

**Process:**
1. When analyzing new legislation, check if any provisions affect:
   - Stablecoin issuers
   - DeFi protocols
   - Custody providers
   - Exchange operators
   - Any specific portfolio company sector
2. Query paradigmdb for relevant holdings:
   ```bash
   call paradigmdb notes_search '{"query":"[sector keyword]"}'
   call paradigmdb db_query '{"query":"SELECT o.name, o.description FROM \"Organization\" o WHERE o.description ILIKE '\''%stablecoin%'\'';"}'
   ```
3. Flag with explanation of potential impact

---

## Jurisdiction Mapping

### Who Has the Pen?

| Issue | Primary Jurisdiction | Key Committees |
|-------|---------------------|----------------|
| Market structure | SEC | Senate Banking, House Financial Services |
| Derivatives | CFTC | Senate/House Agriculture |
| Stablecoins | Mixed (SEC/OCC/Fed) | Banking + Financial Services |
| AML/KYC | FinCEN | Banking, Homeland Security |
| Sanctions | OFAC/Treasury | Banking, Foreign Affairs |
| Tax | Treasury/IRS | Finance, Ways & Means |

### Committee Staff Routing

For a given issue, route to the right staffer:
1. **Committee Counsel** for legislative drafting
2. **Personal Office LD** for member positioning
3. **Leadership Staff** for floor scheduling and whip operations

---

## Internal Accountability

### Log All Hill Interactions

After every Hill meeting or call:
1. Log in Shift as meeting note
2. Record commitments made (by us and by them)
3. Note follow-ups owed
4. Update relevant whip sheet entries
5. Tag portfolio-relevant intel

### Periodic Reports

**Weekly:** Policy team activity summary
**Monthly:** Legislative landscape shifts, whip sheet changes
**Quarterly:** Strategic assessment for leadership

---

## Natural Language Queries

The system should answer questions like:

- "What is Senator X's likely position on the GENIUS Act and why?"
- "Which bills introduced this session could affect our stablecoin portfolio companies?"
- "Who are the key staffers on Senate Banking covering crypto?"
- "What have we heard about stablecoin reserve requirements in the last 90 days?"
- "Generate a briefer for my meeting with Rep Y tomorrow"
- "What is the current whip count on [bill]?"
- "Which states have introduced DeFi-related bills this year?"

---

## Example Session

**User:** Generate a briefer for my meeting with Senator Lummis tomorrow about stablecoin legislation.

**Agent:**
1. Read Policy Explainer Index first for internal analysis
2. Web search for Senator Lummis profile, recent statements, committee assignments
3. Search internal sources:
   ```bash
   call slack search_messages '{"query":"Lummis"}'
   call gsuite gmail_search '{"query":"Lummis"}'
   call paradigmdb notes_search '{"query":"Lummis"}'
   ```
4. Find current stablecoin legislation status
5. Check portfolio companies in stablecoin space
6. Generate briefer using template

---

## Future Integrations

| Integration | Purpose | Priority |
|-------------|---------|----------|
| Plural Policy API | Bill tracking, momentum scores | High |
| Shift direct integration | Portfolio cross-reference | Medium |
| Regulations.gov API | Docket comments, rulemaking tracking | Medium |
