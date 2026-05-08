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
