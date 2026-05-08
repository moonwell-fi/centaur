# Centaur charting — visual style spec

The non-negotiables that define the Centaur look. These rules are baked into
the chart tool. Agents should not override them without a clear reason.

---

## 1. Slack-mobile-readable is THE design constraint

Every chart must read legibly inside Slack's inline mobile preview (~360 px
wide), without hover, without zoom, on both light and dark Slack themes.

| Defaults | Value |
| -------- | ----- |
| Figsize  | `(8.0, 4.5)` (16:9). |
| Save DPI | `200`. |
| PNG dimensions | 1600 × 900 px (lossless retina; Slack expand view ~1024 px wide). |
| Sparkline | `(2.0, 0.5)` × `dpi=200` → 400 × 100 px. |
| KPI tile  | `(3.0, 1.2)` × `dpi=200` → 600 × 240 px. |
| Tick label size | ≥ 9.5 pt at 200 DPI (≈ 21 px source — survives downsampling). |
| Axis label size | 10.5 pt. |
| Title size | 13 pt semibold. |
| Format | PNG only. Slack image blocks reject SVG / WebP / AVIF. |
| Aspect | 16:9 max. **Never go vertical-tall** — Slack mobile crops portrait. |
| Backgrounds | White by default; renders as a clean card on Slack dark mode. |
| Interactivity | None. No hover-only tooltips, no zoomable layers. |

---

## 1.5 Tufte/Cleveland operating rules

These are not aesthetic preferences; they prevent misleading charts.

| Principle | Operational rule |
| --------- | ---------------- |
| Data-ink ratio | Remove any pixel that does not carry data, context, or a needed label. |
| Lie factor | Visual magnitude should match data magnitude; no truncated bars or arbitrary scales. |
| Small multiples | Prefer facets over spaghetti when there are >6-7 series. Share axes when comparison matters. |
| Sparklines | Use for inline trend context beside KPIs; do not use as the only evidence for a claim. |
| Layering/separation | Data is strongest; grid/spines/source are quiet. Use highlight + grey to separate protagonist from context. |
| Multi-functioning elements | Direct labels can replace legends; reference lines can both orient and explain. |
| Cleveland hierarchy | Prefer common-scale position, then length, then area, then angle/colour. |
| Knaflic story rule | Title states the takeaway; subtitle carries units/range/baseline. |

Default chart choice follows this order unless the user asks otherwise:

1. Line for time; horizontal bar/dot for ranking; scatter for relationship; histogram/box/ECDF for distribution.
2. Indexed line for comparing assets or strategies over time.
3. Small multiples instead of crowded overlays.
4. Table or KPI tile when exact values matter more than shape.
5. Pie only when there are 2-3 slices and the whole/part metaphor is the point.

---

## 2. Three modes

| Mode | Background | Title font | When |
| ---- | ---------- | ---------- | ---- |
| `light` (default) | `#F8F9FA` cool near-white | Inter Semibold sans | Default for everything. |
| `dark` | `#0F1115` near-black with hint of blue | Inter Semibold sans | Trader / late-night dashboards; on explicit user request. |
| `editorial` | `#F5F4F0` off-white | Playfair Display Semibold serif | IR memos, long-form essays, policy briefs. |

Set `theme_mode` to `light`, `dark`, or `editorial` when calling
`chart.render_chart`. The chart tool applies the theme for you.

---

## 3. Typography

### Font stack

```
Inter, IBM Plex Sans, Source Sans 3, DejaVu Sans, Liberation Sans, Helvetica, Arial
```

Inter is the primary on light and dark; IBM Plex Sans fallback for PDF
(matplotlib has a known PDF-embedding bug with Inter — issue #29396). Source
Sans 3 is the universal-fallback when neither Inter nor Plex is present.

For monospace blocks (ticker symbols, numerics in tables-as-images):

```
JetBrains Mono, IBM Plex Mono, DejaVu Sans Mono, Menlo, monospace
```

### Sizing scale (at 200 DPI savefig)

| Layer | Size | Weight |
| ----- | ---- | ------ |
| Title (takeaway sentence) | 13 pt | 600 (semibold) |
| Subtitle | 10 pt | 400 (regular) |
| Axis label | 10.5 pt | 400 |
| Tick labels | 9.5 pt | 400 (tabular) |
| Direct line label | 10 pt | 600 |
| Annotation callout | 9 pt | 400, italic |
| Source / footer | 8.5 pt | 400, gray |
| Big number (KPI) | 28-48 pt | 700 |

### Rules

1. **Tabular figures everywhere there are numbers.** `font-variant-numeric: tabular-nums`.
2. **Sentence case for titles.** Title-Case-Like-This is for newspapers from 1955.
3. **Drop the legend, label inline.** When ≤ 4 series, use `cc.direct_label_lines(ax)`.
4. **Numbers in muted gray, not pure black.** Reserve pure black for protagonist data.
5. **No italics on data.** Reserve italics for narrative annotations only.
6. **Numbers in the title are powerful.** "BTC up 38% YTD" beats "BTC price."

---

## 4. Colour

### Categorical (Okabe-Ito — colorblind-safe)

Light mode:

```
#0072B2  blue
#D55E00  vermilion
#009E73  bluish green
#CC79A7  reddish purple
#F0E442  yellow
#56B4E9  sky blue
#E69F00  orange
#000000  black
```

Dark mode (brightened — sky-blue-led, Bloomberg-amber accent):

```
#56B4E9  sky blue
#FFA028  amber
#14F195  mint
#CC79A7  reddish purple
#F0E442  yellow
#E69F00  orange
#D55E00  vermilion
#F0F0F0  off-white
```

### Highlight + grey (the most under-used technique)

For any chart with > 2 series:

```
HIGHLIGHT (light)        #0072B2     # Centaur primary blue
HIGHLIGHT (dark)         #56B4E9     # sky blue
HIGHLIGHT (editorial)    #0F5499     # FT-deep-blue

GREY_MUTED (light)       #C8CDD3     # cool gray
GREY_MUTED (dark)        #4A4F55
GREY_MUTED (editorial)   #B5B0A8
```

Pair with `cc.highlight_one(ax, "<protagonist>")`. Single most consistent
visual signature in the Centaur look.

### Sequential / diverging

| Use | Light | Dark |
| --- | ----- | ---- |
| Sequential ordered | `viridis` (default) or `cmcrameri.batlow` | `magma` (default) or `cmcrameri.batlow` |
| Diverging (zero-centered, finance) | `RdBu_r` (red=down, blue=up) | `RdBu_r` (same) |
| Diverging (zero-centered, science) | `cmcrameri.vik` | `cmcrameri.vik` |

### Gain / loss (semantic, finance)

| Mode | Gain | Loss | Gain (CVD-safe) | Loss (CVD-safe) |
| ---- | ---- | ---- | --------------- | --------------- |
| Light | `#26A69A` (TradingView teal) | `#EF5350` (TradingView coral) | `#118AB2` | `#EF476F` |
| Dark | `#26C9B5` | `#FF6B68` | `#56B4E9` | `#FF6B6B` |
| Editorial | `#2C7A7B` | `#C53030` | `#0F5499` | `#B31147` |

Pair colour with shape (`+` / `−` / arrow) so colourblind users get the signal
even when colour is unavailable.

### Brand colours

Use canonical hex values for common assets and chains where available. Coverage includes:

- **Tokens** — BTC `#F7931A`, ETH `#627EEA`, USDC `#2775CA`, USDT `#26A17B`,
  SOL `#14F195`, BNB `#F3BA2F`, XRP `#00AAE4`, DOGE `#C2A633`, plus another
  ~50 majors (UNI, AAVE, COMP, MKR, CRV, LDO, RPL, PENDLE, GMX, …).
- **Chains** — Bitcoin `#F7931A`, Ethereum `#627EEA`, Arbitrum `#28A0F0`,
  Optimism `#FF0420`, Base `#0052FF`, Polygon `#8247E5`, Solana `#14F195`,
  Avalanche `#E84142`, Polkadot `#E6007A`, Cosmos `#2E3148`, plus L2s
  (Linea, Scroll, zkSync, Starknet, Blast, Mantle, Mode, Zora, Ink, Unichain,
  Soneium, Abstract, Monad, Sonic, …).
- **Protocols** — Uniswap `#FF007A`, Aave `#B6509E`, Compound `#00D395`,
  MakerDAO/Sky `#1AAB9B`, Lido `#00A3FF`, Pendle `#259D6F`, plus others.
- **Stablecoins** — by-class semantic palette (USD-tier green, off-chain blue,
  CDP orange, algo red).
- **Events** — halving amber, fork teal, hack crimson, regulation navy,
  launch green, exchange-collapse gray, vote purple, macro slate.

When a name isn't in the dictionary, the router falls back to a stable
hash-of-name slot in the Okabe-Ito cycle so re-renders don't shuffle.

---

## 5. Layout

### Spines & ticks

- Top + right spines: **always hidden**.
- Left + bottom spines: visible.
- Y-axis ticks: **hidden** (gridlines do the work).
- X-axis ticks: short outward, 0.8 px width.

### Gridlines

- **Horizontal-only** (`axes.grid.axis = "y"`) by default. Vertical gridlines
  reserved for finance contexts where they aid time-axis reading.
- Colour: `#E5E7EB` light / `#2D3137` dark.
- Width: 0.6 px. Always behind the data (`axes.axisbelow = True`).

### Constrained layout, never tight_layout

`figure.constrained_layout.use = True` is the default. Never call
`plt.tight_layout()`.

### Standard layouts

#### Annotated time series

```
┌──────────────────────────────────────────────────────────────────┐
│ Sentence-case takeaway title                ←  TITLE             │
│ Subtitle (units, range, baseline)           ←  SUBTITLE          │
│                                                                   │
│  300 ─                                                            │
│      │                          ╱──ETH ←── direct label (color)  │
│  200 ─               ╱──── ╱                                      │
│      │   ╱──── BTC ←── (gray, indexed)                            │
│  100 ─                                                            │
│      └─┴────┴────┴────┴────┴────┴────                             │
│      Jan  Apr  Jul  Oct  Jan  Apr                                 │
│            ▌────recession band────▐                               │
│                                                                   │
│ Source: CoinGecko · 30 Apr 2026             ←  FOOTER (left)     │
└──────────────────────────────────────────────────────────────────┘
```

#### Candle + volume two-panel

Top panel: candle (3:1 height ratio). Bottom panel: volume bars colored to
match candle direction, ~70 % opacity. Single shared x-axis.

#### Drawdown chart

Two-panel: cumulative returns top, underwater (negative-only filled area)
bottom. Shared x-axis. Loss-color fill at 35 % alpha; max DD annotated.

#### Correlation heatmap

`RdBu_r` diverging palette, vmin=-1, vmax=1, white at 0. Cell labels for
≤ 12 × 12 matrices. Sort by hierarchical clustering when > 6 variables.

---

## 6. Annotation hierarchy

Three layers of text, in order of importance:

1. **Title states the takeaway, not the topic.** "BTC is up 38% YTD, lagging
   gold by 5pts" beats "BTC price."
2. **Direct labels > legend.** When ≤ 4 series, label the line/bar at
   endpoint or top, in the line's colour, semibold.
3. **Callouts for the 1–3 facts the chart should make obvious.** Thin leader
   line + short phrase ("Fed +75bps", "Mt Gox hack").

Data-driven labels (one per row, programmatic) use small text in the data
colour. Annotation layers (curated, 1–3) use a contrasting weight or italic.
Don't mix.

---

## 7. Crypto / finance conventions

- **Log scale for prices** spanning > 2× range. Linear when range < 2× and
  audience wants absolute dollars.
- **Linear scale for returns.**
- **Always rebase** (indexed at 100) when comparing instruments.
- **Volume below price**, never on a dual y-axis.
- **Date axis**: continuous time (no weekend gaps) for crypto; for TradFi,
  suppress weekend/holiday gaps unless intraday gaps carry meaning.
- **Annotate events directly** — halvings, forks, FOMC, ETF approvals,
  exchange collapses. Use the `event` palette.

---

## 8. Anti-patterns (router auto-downgrades)

| Anti-pattern | Router behaviour |
| ------------ | ---------------- |
| Pie with > 3 slices | Auto-downgrades to ranked horizontal bar. |
| Stacked bar with > 5 segments | Aggregates smallest into "Other". |
| Stacked area with > 5 components | Aggregates smallest into "Other". |
| 3D | Always rejected. |
| Dual y-axis | Use indexed line or two-panel layout. |
| Radar / gauge / word cloud | Always rejected. |
| Choropleth with raw counts | Use rates per capita / per area. |
| Truncated y-axis on bars | Bars always start at zero. |

If you have a strong reason to override one of these, set
`intent.extras["allow_anti_pattern"] = True` and document why in the
takeaway. Reviewers will look for this flag.

---

## 9. Image transport

Always pass `alt_text` to `slack upload_file`. The router builds a sensible
default from `intent.takeaway_title` + data shape; override for accessibility-
critical contexts.

```bash
call slack upload_file '{
  "channel": "<CHANNEL>",
  "file_path": "/tmp/chart.png",
  "title": "<takeaway>",
  "alt_text": "<art.alt_text>",
  "thread_ts": "<THREAD_TS>"
}'
```

For Notion + Google Docs, use the existing `gsuite` and `notion` tools'
inline-image methods. The PNG produced by the router renders correctly on all
three surfaces without modification.
