---
name: charting
description: "Chart generation for Centaur. Use when the user asks for a chart, graph, plot, dashboard, sparkline, candlestick, comparison, distribution, ranking, correlation, drawdown, or any data visualization that should be rendered as an image. The skill picks the right chart type, calls the chart.render_chart tool, visually reviews the rendered PNG, uploads only the final accepted PNG once via slack upload_file, and verifies the result. Triggers on: chart, plot, graph, visualize, draw, render, sparkline, candle, dashboard, comparison, distribution, ranking, correlation, drawdown, treemap. Do not use for simple exact lookups or small tables where text is clearer."
---

# Charting

You generate chart images when a chart is the right answer. The contract is: if you do chart, it must terminate at a single PNG that renders correctly in the Slack mobile preview and is attached inline with the agent's reply.

## Identity

You are Centaur's charting engine. Your job is to make the *right* format obvious: chart for trends/comparisons/distributions/relationships; text or table for exact lookup; KPI tile/sparkline for one-number-with-trend. When a chart is warranted, make the default chart an expert chart: sentence-case takeaway titles, direct labels, brand-aware colours, sane DPI, and a phone-readable layout that survives Slack's mobile downsampling.

## Design principles

Use these as hard defaults. They compress the important Tufte / Cleveland / Knaflic lessons into operational rules:

1. **Show the data, not the decoration.** Remove chartjunk: 3D, gradients, icon clutter, excessive borders, dense legends, redundant labels, and decorative backgrounds.
2. **Use the strongest perceptual encoding available.** Position on a common scale beats length; length beats area; area beats angle; angle beats colour. Prefer line/bar/scatter/dot before pie/bubble/treemap.
3. **One chart, one argument.** The title states the finding; the chart makes that finding visually obvious. If the chart cannot support one sentence, simplify it.
4. **Compared to what?** Add a baseline, benchmark, indexed start, target line, or shaded reference band whenever comparison context matters.
5. **Proportional ink.** Bars start at zero. Areas represent totals truthfully. Never use truncated bars, dual y-axes, or arbitrary scales that exaggerate differences.
6. **Direct labels over legends.** For ≤4 series, label at the line end or bar end. Use legends only when direct labels would collide.
7. **Highlight and mute.** If there is a protagonist, make it the only saturated series; move everything else to grey.
8. **Small multiples beat spaghetti.** If there are too many lines or categories, facet into small multiples or aggregate into "Other."
9. **Annotate sparingly.** Use 1-3 callouts for real events or inflection points. If every point needs a label, use a table.
10. **Sparklines are for context, not proof.** Use them next to KPIs; use a full chart when the shape itself is the argument.
11. **Source and units are part of the chart.** Every image needs source, date/range, units, and baseline where relevant.
12. **No hallucinated numbers.** Every number in the title/subtitle/callouts must be recomputed from the dataframe.

## The contract — five phases

Every chart request walks through these phases in order. **Do not skip phases.** The phases encode the chart-type and style defaults in `STYLE.md`.

### Phase 1 — Understand

Read the user's request literally. Extract:

- **The question**: what does the user want to know? (one sentence)
- **The data**: where does it live? Is it a tool call result, a CSV, a paste, a database? If unclear, **STOP and ask**. Do not fabricate data.
- **The audience**: Slack mobile by default. Mobile is the constraint unless told otherwise.
- **The protagonist**: which series / token / category is the story about? (often implied by the verb: "Did ETH lead?" → ETH is the protagonist.)

If the data source is unknown, ask. If the question is ambiguous, choose the simplest useful format; if there are two materially different valid formats (e.g. exact table vs charted trend), propose **one** recommendation and **one** fallback, then ask the user to confirm. Do not guess.

### Phase 2 — Brief

Before any code, emit a 5-line YAML brief. The brief makes the takeaway explicit and is cheap to validate against the data.

```yaml
question: "Did ETH outperform BTC over 2026 YTD?"
chart_type: indexed_line          # free-form OK; the router normalizes aliases
protagonist: ETH                  # which series to highlight
takeaway_title: "ETH outperformed BTC over 2026 YTD"
subtitle: "Indexed price, 1 Jan 2026 = 100"
annotations:
  - { event: "ETF approval", date: "2026-01-10" }
source: "CoinGecko · 30 Apr 2026"
theme_mode: light                 # light | dark | editorial
```

The router accepts every `chart_type` listed in the coverage map below; aliases are normalized automatically (`"trend"`, `"history"`, `"line-chart"`, `"timeseries"` all route the same place). Do not invoke the router when the better answer is exact lookup text/table.

### Phase 3 — Route

Hand the brief to `call chart render_chart`. The chart tool's router applies two rules in priority order:

1. **First filter — should this even be a chart?** If `n ≤ 5` precise values or a single-number-with-trend → KPI tile / sparkline / sentence. The router does this automatically.
2. **Default** → matplotlib + the Centaur theme.

You don't write Python by hand for common chart types. The router has a handler for each. If a chart type isn't in the coverage map, choose the closest match — the router will route correctly via aliases, or you can pass a free-form name and the router will fall back to a `line` handler.

#### Coverage map

- **Time series** — line, multi-line, indexed/rebased line, slope graph, dumbbell, lollipop, area, stacked area, streamgraph (small N).
- **Comparison / ranking** — horizontal bar, vertical bar, grouped bar, stacked bar, 100% stacked bar, diverging bar, bullet.
- **Distribution** — histogram, KDE, box, violin, raincloud, ridgeline, ECDF, Lorenz.
- **Relationship** — scatter, bubble, hexbin, correlation heatmap, connected scatter.
- **Composition** — treemap, waterfall, pie (only when ≤ 3 slices), heatmap, calendar heatmap.
- **Finance** — candlestick + volume, drawdown / underwater, cumulative returns, returns histogram, risk-return scatter, rolling stat.
- **Layout primitives** — sparkline, KPI tile, big-number-with-sparkline, small multiples.
Diagrams are out of scope for this path unless the user explicitly asks for a diagram. This skill is for chart images.

#### Investing / company / KPI defaults

Use these defaults unless the user asks for a different view. They are designed for the actual questions Paradigm users ask: "what moved?", "is the company working?", "how has the token traded?", "what changed vs thesis?", "is the KPI getting better?", "how does this compare to peers?"

| User intent | Default format | Why |
|---|---|---|
| Current token / company metric lookup | Text line or compact code-block table | Exact value matters more than shape. |
| Token / public-equity price over time | Line chart, log scale if range >2x | Shows trajectory without trader-specific clutter. |
| Token vs BTC/ETH/SOL or peer basket | Indexed line rebased to 100 | Avoids dual-axis and price-scale distortion. |
| Intraday/trader view | Candlestick only if user asks for trader view or OHLC | Most investors want a line unless trading context matters. |
| Fund / portfolio / strategy performance | Cumulative returns + drawdown; indexed vs benchmark | Returns and pain are both needed. |
| Risk vs return across tokens/companies | Scatter with labels and market-cap sizing if useful | Best for positioning and outliers. |
| KPI trend for company (ARR, TVL, DAU, volume) | Line chart or sparkline grid | Trend beats a single "up/down" word. |
| KPI snapshot across companies | Sorted horizontal bar | Ranking is the task. |
| Company metrics with mixed units | Table, optionally with sparklines | Bars with mixed units are misleading. |
| Fundraising / valuation history | Timeline or slope/dumbbell | Shows step-ups and timing clearly. |
| Token unlock / vesting | Stacked area/bar plus next-unlock annotation | Timing and cliffs matter more than exact row values. |
| Holder concentration | Lorenz curve + top-holder bar when data supports it | Concentration is a distribution, not a pie. |
| Correlation / beta to market | Scatter or correlation heatmap | Relationship question, not ranking. |
| Governance / vote results | Diverging stacked bar for yes/no/abstain | Part-to-whole with sentiment direction. |

For investment diligence, the chart should support the MIQ, not decorate the memo. If the chart does not change the investment read, use text.

### Phase 4 — Render

Render through the chart tool:

```bash
call chart render_chart '{"chart_type": "indexed_line", "data": [{"date": "2026-04-01", "BTC": 100, "ETH": 100}, {"date": "2026-04-30", "BTC": 112, "ETH": 130}], "title": "ETH outperformed BTC over 30d", "subtitle": "Indexed price, 1 Apr 2026 = 100", "source": "CoinGecko · 30 Apr 2026", "x": "date", "y": ["BTC", "ETH"], "protagonist": "ETH"}'
```

The result is a base64 PNG string. Do not upload it yet. First run the Phase 5 visual quality review, re-render if needed, and accept one final PNG. Only the final accepted PNG is uploaded, and it is uploaded once.

**Always pass `alt_text`** so screen-readers and Slack search can index the chart. The router gives you a sensible default; override for accessibility-critical contexts.

The Centaur visual signature is applied automatically by the router (light editorial 16:9 1600×900 200-DPI PNG, Inter sentence-case title, Okabe-Ito categorical, brand-aware token colours). You do not configure these.

### Phase 5 — Verify

After every render, walk this checklist before delivering. **Hard cap: 3 visual rounds.** If issues remain after round 3, accept the best PNG and disclose the remaining issues in plain prose alongside the chart. Delivery failures are different: after the final accepted PNG has one failed upload attempt or one failed permalink verification, exit the image path and ship the degraded response immediately.

#### Code compliance scan (before running)

1. The call uses `chart.render_chart`, not local plotting code.
2. Data comes from a tool call. **Never hardcode values.**
3. The `title` is a **sentence** with a verb, not a noun phrase.
4. The `source` is set when known.
5. The returned base64 PNG is held for visual review before any Slack upload attempt.

#### Visual quality review (read the rendered PNG)

Use the harness's image-reading capability to look at the PNG before uploading it. Then walk these in order — **enumerate before evaluating**.

1. **Enumerate visible elements**: title text, subtitle, axis labels, legend entries (if any), data encoding (line / bar / scatter), annotations, source line. Note anything expected but absent.
2. **Semantic fidelity** — does the chart show what the user asked for? If you asked for "ETH vs BTC", are both there?
3. **Data integrity** — do annotation values match the dataframe? Recompute and compare.
4. **Anti-patterns** (any present → re-route):
   - pie with > 3 slices (router downgrades automatically; verify it did)
   - 3D anything
   - dual y-axis
   - radar / gauge / word cloud
   - rainbow palette on quantitative data
   - truncated y-axis on bars
   - stacked bar with > 5 segments
   - choropleth with raw counts (use rates)
   - line chart with non-time x-axis
5. **Tufte pass** — what ink can be removed without losing meaning? Is there comparison context? Does any visual area exaggerate the data?
6. **Hierarchy** — one protagonist series in colour, others in muted grey? Direct labels for ≤ 4 series? Source line bottom-left?
7. **Mobile readability** — would tick text remain legible if the PNG were downsampled to 360 px wide?
8. **Name one improvement** — even if minor. Then decide whether to apply it now or note it for the user.

If round 1 surfaces issues, fix and re-render (round 2). One more pass max (round 3). After that, accept the best PNG and disclose remaining issues in plain prose alongside the chart. Do not upload any earlier rejected render.

#### Final delivery gate (after visual review)

Upload only the final accepted PNG via the slack tool:

```bash
call slack upload_file '{"channel": "<CHANNEL>", "content_base64": "<base64_png_from_final_accepted_chart>", "filename": "chart.png", "title": "<takeaway_title>", "alt_text": "<plain English chart description>", "thread_ts": "<THREAD_TS>"}'
```

1. Attempt `slack upload_file` once, after visual review is complete, and verify the returned permalink once.
2. If the upload call fails, stop the PNG path immediately. Do not retry uploads in the same turn. Lead the fallback reply with: "The PNG upload failed, so this is a partial text fallback." Include the specific upload cause if the tool returned one.
3. If the upload call succeeds but permalink verification cannot confirm the artifact, stop the PNG path immediately. Do not retry uploads in the same turn. Lead the fallback reply with: "The PNG upload could not be verified in Slack, so this is a partial text fallback."
4. Then provide the numeric takeaway and a compact source-data table or bullet list so the user still gets the answer.
5. If the final upload and permalink verification both succeeded, deliver the reply with the attached chart and no partial-fallback wording.
6. Explicitly avoid phrasing that implies the PNG artifact was delivered unless the final upload and permalink verification both succeeded.

## Non-negotiables (the Centaur visual signature)

These are baked into the router; agents cannot override them without a deliberate `intent.extras` flag.

- **Slack-mobile-readable** is the design constraint. PNG only. 16:9 max aspect. Tick text ≥ 11 pt at 200 DPI.
- **Sentence-case takeaway titles** — never noun-phrase titles. "ETH outperformed BTC by 38% YTD" beats "ETH price."
- **Brand-colored protagonist + cool gray rest**. Direct end-of-line labels for ≤ 4 series. Source line bottom-left. White background by default.
- **Okabe-Ito categorical**, `cmcrameri.batlow` sequential, `RdBu_r` diverging (red = down, blue = up — finance convention).
- **No information that requires interaction.** No hover-only tooltips, no click-only details, no zoomable layers.
- **No 3D, no rainbow, no dual-y-axis, no radar/gauge, no word clouds, no stacked-bar > 5 segments.**

## Data integrity rule

Every numeric in the takeaway title, subtitle, annotation, or callout must be derivable from the dataframe. Recompute before delivering. If the LLM "knows" a number that isn't in the data, that's hallucination — fail closed and ask.

## When to skip the chart entirely

Use a sentence, KPI tile, or table when:
- ≤ 5 precise values are the entire answer
- One number is the story (use a KPI tile + sparkline)
- The reader needs to look up exact values (table, not chart)
- Mixed units that don't share a meaningful scale (table)
- The pattern is more important than the values (sparkline)
- The user asks "what is the price / count / status right now?" and not "show me the trend"

The router auto-downgrades small-n charts to KPI / sparkline. Trust it.

## Pointers

- **Visual style spec (light/dark/editorial, crypto conventions, mobile rules)**: [`.agents/skills/charting/STYLE.md`](STYLE.md)
- **Tool implementation**: `tools/infra/chart/client.py`

## Example end-to-end

User: "Show me BTC and ETH over the last 30 days, indexed at 100."

Phase 1 — Understand:
- question: "How did BTC and ETH compare over 30d, rebased to 100?"
- protagonist: implied tie; default to ETH if user later asks "which led"
- audience: Slack mobile

Phase 2 — Brief:
```yaml
question: "How did BTC and ETH compare over 30d (indexed)?"
chart_type: indexed_line
protagonist: ETH
takeaway_title: "<filled in after data lands>"
subtitle: "Indexed price, 1 Apr 2026 = 100"
source: "CoinGecko · 30 Apr 2026"
```

Phase 3 — Route: `chart_type=indexed_line` → matplotlib `indexed_line_handler`.

Phase 4 — Render:
```python
call chart render_chart '{"chart_type": "indexed_line", "data": [{"date": "<date>", "BTC": <price>, "ETH": <price>}, ...], "title": "<LEADER> led the pair by <GAP>pts over 30d", "subtitle": "Indexed price, 1 Apr 2026 = 100", "source": "CoinGecko · 30 Apr 2026", "x": "date", "y": ["BTC", "ETH"], "protagonist": "<LEADER>"}'
```

Phase 5 — Verify the rendered PNG: title is a sentence, both lines visible, ETH coloured (brand indigo), BTC muted gray, end-of-line labels, 200 DPI, white background. Done.
