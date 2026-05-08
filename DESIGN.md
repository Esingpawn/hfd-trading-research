# Design System - HFD Trading Research System

## Product Context

- **What this is:** HFD is a crypto signal research and paper trading system built around HFD Pro snapshots, strategy decisions, runtime diagnostics, and paper trade review.
- **Who it is for:** A hands-on crypto operator who needs to monitor signal quality, understand why trades did or did not open, and review sample accumulation before any live trading step.
- **Space:** Quant research dashboard, trading operations console, paper trading journal.
- **Project type:** Data-dense web app dashboard. Not a marketing site.

## Aesthetic Direction

- **Name:** Dark Flow Radar Console.
- **Direction:** Dark, low-glare, operational, data-first.
- **Mood:** The interface should feel like a serious trading radar desk: calm, layered, precise, and easy to keep open for long sessions.
- **Principle:** Backgrounds and panels stay quiet. Color only appears when it communicates live state, direction, risk, or priority.

### Reference

- The layout and radar feeling can borrow restraint from `https://michill.ai/#radar-panel`.
- Do not copy the bright or light pink visual direction into production.
- Use only a small rose accent for identity and hierarchy. Cyan, mint, amber, and red remain semantic signal colors.

### Must Avoid

- Bright light surfaces, high-saturation pink panels, glossy glassmorphism, and candy neon.
- Weak region separation where cards blend into the page.
- Single-hue blue or purple dashboard palettes.
- Decorative hero or marketing sections in the app shell.

## Typography

### Font Roles

- **Display and section headings:** `Sora`, `Aptos Display`, `PingFang SC`, `Microsoft YaHei`, `Noto Sans SC`, system sans.
- **Body and UI:** `PingFang SC`, `Microsoft YaHei`, `Noto Sans SC`, `Segoe UI`, system sans.
- **Data and tables:** `JetBrains Mono`, `SFMono-Regular`, `Consolas`, monospace.

### Loading

For local prototypes, Google Fonts may be used:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700;800&family=Sora:wght@500;600;700;800&display=swap" rel="stylesheet">
```

For server deployment, the dashboard must still look correct without external font loading:

```css
--font-display: "Sora", "Aptos Display", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif;
--font-ui: "PingFang SC", "Microsoft YaHei", "Noto Sans SC", "Segoe UI", system-ui, sans-serif;
--font-data: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
```

### Type Scale

- `xs`: 11px, auxiliary metadata, table hints.
- `sm`: 12px, chips, labels, table cells.
- `base`: 13px, primary dashboard body.
- `md`: 15px, compact section headings.
- `lg`: 18px, card headline numbers.
- `xl`: 24px, KPI values.
- `2xl`: 32px, primary screen title or leading market callout.

Rules:

- Use `font-variant-numeric: tabular-nums` globally.
- Do not use negative letter spacing.
- Use `700` or `800` sparingly for headers and status labels.
- Table numbers use the data font where practical.

## Color

### Approach

The system uses matte dark layers with clear depth steps. The final palette is intentionally low-glare: muted teal for live signal, rose for identity emphasis, mint for long/success, red for short/risk, amber for warning, blue for links and neutral information.

### Core Palette

| Token | Hex / Value | Usage |
|---|---:|---|
| `--bg-root` | `#080B0E` | App background |
| `--bg-depth` | `#0D1115` | Deep background shift |
| `--panel` | `rgba(17, 23, 27, .90)` | Large panels |
| `--panel-strong` | `rgba(23, 31, 36, .96)` | Elevated panels and headers |
| `--surface-card` | `rgba(22, 30, 35, .96)` | KPI cards and tool surfaces |
| `--surface-muted` | `rgba(14, 19, 23, .88)` | Nested rows, cells, table headers |
| `--line` | `rgba(203, 224, 218, .13)` | Normal borders |
| `--line-strong` | `rgba(203, 224, 218, .24)` | Focus and selected borders |
| `--text-main` | `#EDF4F1` | Primary text |
| `--text-muted` | `#A5B3AF` | Secondary text |
| `--text-faint` | `#6F7D7A` | Timestamps and low-priority notes |
| `--signal` | `#43D6C4` | Live state, active nav, primary action |
| `--rose` | `#D66F94` | Identity accent, limited emphasis |
| `--mint` | `#5EE0A0` | Long direction, success |
| `--amber` | `#D7A84F` | Warning, waiting, stale |
| `--blue` | `#75A7FF` | Links and information |
| `--bad` | `#F0657F` | Short direction, danger, loss |

### CSS Tokens

```css
:root {
  color-scheme: dark;
  --bg-root: #080b0e;
  --bg-depth: #0d1115;
  --panel: rgba(17, 23, 27, .90);
  --panel-strong: rgba(23, 31, 36, .96);
  --surface-card: rgba(22, 30, 35, .96);
  --surface-muted: rgba(14, 19, 23, .88);
  --line: rgba(203, 224, 218, .13);
  --line-strong: rgba(203, 224, 218, .24);
  --text-main: #edf4f1;
  --text-muted: #a5b3af;
  --text-faint: #6f7d7a;
  --signal: #43d6c4;
  --rose: #d66f94;
  --mint: #5ee0a0;
  --amber: #d7a84f;
  --blue: #75a7ff;
  --bad: #f0657f;
  --radius-sm: 4px;
  --radius-md: 6px;
  --radius-lg: 8px;
}
```

### Surface Hierarchy

1. **Root background:** near-black, with only a subtle grid or tonal shift.
2. **Large panels:** `--panel`, 1px `--line` border, low shadow.
3. **Cards and tool surfaces:** `--surface-card`, stronger edge contrast than the page.
4. **Rows, cells, callouts:** `--surface-muted`, table-like separation instead of nested decorative cards.
5. **State accents:** cyan, mint, amber, red, and limited rose.

## Spacing

Use a 4px spacing grid.

| Token | Value | Usage |
|---|---:|---|
| `--space-1` | 4px | Icon gaps, dense inline spacing |
| `--space-2` | 8px | Button gaps, table cell x padding |
| `--space-3` | 12px | Compact card padding |
| `--space-4` | 16px | Standard panel padding |
| `--space-5` | 20px | Section spacing |
| `--space-6` | 24px | Page gutters and major blocks |
| `--space-8` | 32px | Large separation between dashboard bands |

Density rules:

- Default density is compact.
- Tables use 34px to 38px row height.
- Toolbar height is 44px to 56px.
- Top navigation height is 60px to 64px on desktop.
- KPI cards should stay between 150px and 176px high.

## Layout

- Use a constrained wide shell: `max-width: 1860px`, page padding `24px` desktop, `12px` to `14px` mobile.
- Prefer CSS grid for KPI rows, split panels, and data regions.
- Use full-width dashboard bands or unframed layouts.
- Avoid decorative page sections and nested cards.
- Primary app views should optimize scanning: status bar, KPI strip, market table, candidate queue, paper trading stats, diagnostics.

### Recommended Dashboard Grid

- Top bar: brand, nav, runtime health, Telegram status, collector status, local time.
- Row 1: 4 KPI cards for snapshots, paper exposure, signal count, backtest score.
- Row 2: market opportunity table and active candidate radar.
- Row 3: paper trade ledger and backtest ranking.
- Persistent log line below the main data regions.

## Components

### Buttons

- Height: 32px compact, 36px standard.
- Radius: 6px.
- Primary: muted cyan fill with dark text.
- Secondary: surface fill with strong border.
- Danger: transparent fill, red or rose border and text.
- Use hover states that increase border contrast, not glow intensity.

### Chips

- Use chips for status, direction, process health, sample state, and risk gates.
- Radius may be full pill for chips only.
- Dot indicators should be 7px or 8px.
- Dot glow must be restrained; use halos, not bright neon bloom.

### Cards and Panels

- Radius: 8px maximum.
- Border: 1px solid `--line`.
- Shadow: low, typically `0 16px 40px rgba(0,0,0,.28)`.
- Borders and fill contrast should do more work than shadow.
- Do not place decorative cards inside cards. Use table cells, dividers, or grouped rows for internal structure.

### Tables

- Header text: 11px or 12px, muted.
- Body text: 12px or 13px.
- Numeric columns: data font, right aligned where useful.
- Use row hover only to support inspection.
- Use sticky headers for long scrollable tables where needed.

### Forms and Controls

- Inputs use panel background, 1px border, 6px radius.
- Toggles for binary settings.
- Segmented controls for modes such as dry-run/live paper scan, compact/expanded, all/open/closed.
- Sliders only for numeric thresholds where visual adjustment helps.

## Data Visualization

- Use thin strokes, low-fill charts, and direct labels.
- Do not use rainbow palettes.
- Use cyan for current value, rose only for identity accents, mint/red for PnL direction, amber for warning state.
- Show sample size beside any win rate or profit factor.

## Motion

- Motion is minimal and functional.
- Use 120ms to 180ms transitions for hover, active states, table row inspection, and panel reveal.
- Avoid looping animation except small live status pulses.
- No flashy trading-game motion, confetti, or animated gradients.

## Texture and Material

- Matte graphite surfaces.
- Subtle grid or scanline texture may appear in the main background under 5% opacity.
- Do not use visible decorative orbs or bokeh blobs.
- Rose accents should appear as thin lines, badges, or small highlights, not large glowing fills.

## Accessibility

- Do not rely on color alone for direction or risk. Pair color with labels such as `LONG`, `SHORT`, `观察`, `等待确认`, or similar text.
- Text contrast should target WCAG AA.
- Focus states use cyan outline plus stronger border.
- Button and chip labels must not overflow on mobile. Use wrapping or shorter labels before shrinking below 11px.

## Implementation Notes

- Put all design tokens in `:root` and reuse them through the dashboard.
- Keep card radius at 8px or less.
- Use the final dark palette above in production. Do not reintroduce the earlier bright pink/light preview direction.
- Improve region separation through background depth, borders, section headers, and table-like cells.
- Future UI changes must read this file before choosing colors, type, spacing, or component styles.

## Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-08 | Replaced the earlier Quiet Quant Terminal direction with Dark Flow Radar Console | User feedback showed the lighter pink direction was too visually stimulating and panel hierarchy was unclear. |
| 2026-05-08 | Standardized on matte dark surfaces with cyan, rose, mint, amber, blue, and red | Keeps the michill-style radar influence while making the dashboard usable for long trading sessions. |
| 2026-05-08 | Defined explicit surface hierarchy | Fixes the issue where regions and cards were hard to distinguish. |
