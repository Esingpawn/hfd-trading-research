# Frontend migration

The current production dashboard remains `app/web/dashboard.html`.

The new frontend scaffold lives under `web/` and uses:

- Vite
- React
- TypeScript
- ECharts
- Lightweight Charts
- lucide-react

Run locally:

```bash
cd web
npm install
npm run dev
```

The Vite dev server proxies API calls to `http://127.0.0.1:8000`.

Migration rule: do not remove the existing FastAPI dashboard until the React frontend covers market, signals, experiments, paper trading, governance, and system health.

## Updated direction

The dashboard should move to TypeScript / React. The single-file FastAPI HTML page is now a compatibility panel, not the long-term UI architecture.

The React dashboard should be built around fixed backend contracts instead of copying the old table-heavy layout. The first primary contract is:

```text
GET /darkflow/decision-cards
```

This endpoint represents the new core darkflow path:

```text
DarkflowInteraction v2 -> DecisionCard -> anti-repaint gate -> isolated v2 shadow paper -> paper/live promotion
```

Legacy feature candidate reports and `shadow_feature_candidates_v1` remain visible only as `Legacy/Control` evidence. They should not be first-screen decision material.

## React information architecture

The React app should converge on these pages:

1. Command Center: system status, data health, risk gates, top darkflow decision cards.
2. Candidate Trades: full DecisionCard queue with entry, stop, TP, RR, confirmations, blockers.
3. Signal Console: darkflow indicator layers and normalized signal events.
4. Backtest Lab: anti-repaint status, walk-forward reports, stress tests.
5. Strategy Lab: playbook lifecycle and promotion gates.
6. Risk & Execution: paper/live safety state, orders, positions, kill switches.
7. Journal: trade review, attribution, and good-loss/bad-loss classification.

## Retire criteria for the old HTML dashboard

Do not remove `app/web/dashboard.html` until the React app can show:

- `/darkflow/decision-cards` with blockers and promotion gates.
- Data quality and anti-repaint status.
- Darkflow v2 interaction quality and playbook readiness.
- Isolated v2 shadow-paper performance.
- Legacy/control labels for old feature and shadow reports.
- Paper trading and risk safety state.

When those are covered, `/dashboard` can serve the built React bundle and the old HTML file can move to an archive path.
