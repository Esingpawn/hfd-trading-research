import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, Database, FlaskConical, LineChart, Settings } from "lucide-react";
import "./styles.css";

type Summary = {
  snapshots: number;
  prices: number;
  decisions: number;
  open_trades: number;
};

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function App() {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchJson<Summary>("/system/summary")
      .then(setSummary)
      .catch((err) => setError(String(err)));
  }, []);

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">HFD</div>
        <button><Activity size={18} /> 市场</button>
        <button><LineChart size={18} /> 信号</button>
        <button><FlaskConical size={18} /> 实验</button>
        <button><Database size={18} /> 数据</button>
        <button><Settings size={18} /> 设置</button>
      </aside>
      <section className="content">
        <header>
          <h1>HFD Research Dashboard</h1>
          <span>React frontend scaffold</span>
        </header>
        {error && <div className="status error">{error}</div>}
        <div className="grid">
          <Metric label="Snapshots" value={summary?.snapshots} />
          <Metric label="Prices" value={summary?.prices} />
          <Metric label="Decisions" value={summary?.decisions} />
          <Metric label="Open Trades" value={summary?.open_trades} />
        </div>
        <div className="panel">
          <h2>迁移状态</h2>
          <p>当前 React 面板是工程化骨架，旧版 FastAPI dashboard 仍保留为主面板。</p>
        </div>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value?: number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value ?? "..."}</strong>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
