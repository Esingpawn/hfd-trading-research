import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Database,
  FlaskConical,
  Gauge,
  LineChart,
  RefreshCcw,
  ShieldCheck,
} from "lucide-react";
import "./styles.css";

type Summary = {
  snapshots: number;
  prices: number;
  decisions: number;
  open_trades: number;
  mode: string;
  latest?: {
    signal_snapshot_at?: string | null;
    price_snapshot_at?: string | null;
    decision_at?: string | null;
  };
};

type DataQuality = {
  status: "ok" | "warning" | "error" | string;
  issues: Array<{ code: string; severity: string; message: string }>;
  completeness?: Record<string, unknown>;
  prices?: Record<string, unknown>;
  features?: Record<string, unknown>;
};

type DecisionCardReport = {
  card_count: number;
  scanned_interactions: number;
  cards: DecisionCard[];
  thresholds: { min_quality_score: number; min_rr_ratio: number };
  policy: Policy;
};

type TradeCandidateReport = {
  candidate_count: number;
  candidates: TradeCandidate[];
  policy: Policy;
};

type TradeCandidate = {
  candidate_key: string;
  strategy_id: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  interval: string;
  direction: "long" | "short" | string;
  setup_type: string;
  market_state: string;
  setup_time: string | null;
  entry_price: number;
  stop_price: number;
  target_price: number;
  rr_ratio: number;
  quality_score: number;
  status: string;
  promotion_status: string;
  anti_repaint_status: string;
  shadow_status: string;
  paper_eligible: boolean;
  live_eligible: boolean;
  blockers: string[];
  promotion_blockers: string[];
  supporting_signals: string[];
};

type DecisionCard = {
  card_id: string;
  strategy_id: string;
  strategy_name: string;
  symbol: string;
  timeframe: string;
  interval: string;
  direction: "long" | "short" | string;
  setup_type: string;
  market_state: string;
  setup_time: string | null;
  entry_plan: {
    trigger: string;
    planned_entry: number;
    planned_stop: number;
    take_profit_levels: Array<{ label: string; price: number; source: string }>;
    max_hold_bars: number;
  };
  scores: { rule_score: number; quality_score: number; model_win_prob: number | null; expected_R: number | null };
  risk: { rr_ratio: number; stop_distance_pct: number; target_distance_pct: number };
  supporting_signals: string[];
  blocking_risks: string[];
  risk_gate: {
    status: "shadow_candidate" | "research_blocked" | string;
    blockers: string[];
    paper_eligible: boolean;
    live_eligible: boolean;
    promotion_blockers: string[];
  };
  observed_backtest_result: { exit_reason: string | null; r_multiple: number | null; mfe: number; mae: number };
};

type DarkflowBacktest = {
  quality_interaction_count?: number;
  interaction_count?: number;
  quality_stats?: { win_rate?: number | null; profit_factor?: number | null; max_drawdown?: number | null };
  candidate_playbook_count?: number;
  watchlist_playbook_count?: number;
  generated_at?: string | null;
  policy?: Policy;
};

type ShadowStats = {
  total_trades?: number;
  open_trades?: number;
  closed_trades?: number;
  win_rate?: number | null;
  profit_factor?: number | null;
  policy?: Policy;
};

type Policy = {
  opens_live_orders?: boolean;
  opens_paper_trades?: boolean;
  used_for_opening_decisions?: boolean;
  lineage?: { lineage: string; legacy_control?: boolean; is_primary_darkflow_path?: boolean };
};

type LoadState = {
  summary: Summary | null;
  quality: DataQuality | null;
  cards: DecisionCardReport | null;
  candidates: TradeCandidateReport | null;
  darkflow: DarkflowBacktest | null;
  shadow: ShadowStats | null;
};

type LoadErrors = Partial<Record<keyof LoadState, string>>;

const EMPTY_STATE: LoadState = {
  summary: null,
  quality: null,
  cards: null,
  candidates: null,
  darkflow: null,
  shadow: null,
};

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function App() {
  const [data, setData] = useState<LoadState>(EMPTY_STATE);
  const [errors, setErrors] = useState<LoadErrors>({});
  const [loading, setLoading] = useState(false);

  async function refresh() {
    setLoading(true);
    setErrors({});
    await Promise.all([
      loadSection("summary", () => fetchJson<Summary>("/system/summary")),
      loadSection("quality", () => fetchJson<DataQuality>("/data/quality-report")),
      loadSection("cards", () => fetchJson<DecisionCardReport>("/darkflow/decision-cards?limit=12")),
      loadSection("candidates", () => fetchJson<TradeCandidateReport>("/darkflow/trade-candidates?limit=12")),
      loadSection("darkflow", () => fetchJson<DarkflowBacktest>("/darkflow/interactions/backtest/latest")),
      loadSection("shadow", () => fetchJson<ShadowStats>("/shadow-paper/stats")),
    ]);
    setLoading(false);
  }

  async function loadSection<Key extends keyof LoadState>(key: Key, loader: () => Promise<LoadState[Key]>) {
    try {
      const value = await loader();
      setData((current) => ({ ...current, [key]: value }));
    } catch (err) {
      setErrors((current) => ({ ...current, [key]: errorText(err) }));
      setData((current) => ({ ...current, [key]: null }));
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const topCard = data.cards?.cards[0];
  const riskStatus = useMemo(() => deriveRiskStatus(data), [data]);

  return (
    <main className="appShell">
      <aside className="sidebar">
        <div className="brandBlock">
          <div className="brandMark">HFD</div>
          <span>Darkflow Command</span>
        </div>
        <nav>
          <button className="active"><Gauge size={18} /> 指挥中心</button>
          <button><LineChart size={18} /> 候选交易</button>
          <button><BarChart3 size={18} /> 回测验证</button>
          <button><FlaskConical size={18} /> 策略实验</button>
          <button><Database size={18} /> 数据健康</button>
        </nav>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">Official tutorial semantics first</p>
            <h1>暗流交易指挥台</h1>
          </div>
          <div className="topActions">
            <StatusPill label={riskStatus.label} tone={riskStatus.tone} />
            <button className="iconButton" onClick={refresh} disabled={loading} aria-label="刷新">
              <RefreshCcw size={17} />
              {loading ? "刷新中" : "刷新"}
            </button>
          </div>
        </header>

        {Object.keys(errors).length > 0 && <LoadErrorBanner errors={errors} />}

        <section className="metricGrid">
          <Metric title="系统模式" value={modeText(data.summary?.mode)} detail="live disabled" tone="info" />
          <Metric title="数据状态" value={qualityText(data.quality?.status)} detail={`${data.quality?.issues.length ?? 0} issues`} tone={data.quality?.status === "ok" ? "good" : "warn"} />
          <Metric title="暗流质量样本" value={`${fmt(data.darkflow?.quality_interaction_count, 0)} / ${fmt(data.darkflow?.interaction_count, 0)}`} detail={`PF ${fmt(data.darkflow?.quality_stats?.profit_factor, 2)}`} tone="good" />
          <Metric title="持久候选" value={fmt(data.candidates?.candidate_count, 0)} detail={lineageText(data.candidates?.policy)} tone="info" />
        </section>

        <section className="heroGrid">
          <Panel title="最高优先级交易卡" subtitle="research-only decision card">
            {topCard ? <DecisionCardView card={topCard} featured /> : <Empty text="暂无暗流 v2 交易卡" />}
          </Panel>
          <Panel title="风控闸门" subtitle="promotion blockers">
            <GateList card={topCard} quality={data.quality} />
          </Panel>
        </section>

        <section className="contentGrid">
          <Panel title="候选交易队列" subtitle={`${data.cards?.card_count ?? 0} cards from v2 interactions`}>
            <div className="cardList">
              {(data.cards?.cards ?? []).slice(0, 8).map((card) => <DecisionCardView key={card.card_id} card={card} />)}
              {!data.cards?.cards.length && <Empty text="等待 /darkflow/decision-cards 返回候选" />}
            </div>
          </Panel>
          <Panel title="暗流 v2 质量" subtitle={timeText(data.darkflow?.generated_at)}>
            <QualityPanel darkflow={data.darkflow} cards={data.cards} />
          </Panel>
        </section>

        <section className="contentGrid single">
          <Panel title="候选生命周期" subtitle={`${data.candidates?.candidate_count ?? 0} materialized candidates`}>
            <CandidateLifecycle candidates={data.candidates?.candidates ?? []} />
          </Panel>
        </section>
      </section>
    </main>
  );
}

function LoadErrorBanner({ errors }: { errors: LoadErrors }) {
  const labels: Record<keyof LoadState, string> = {
    summary: "系统摘要",
    quality: "数据质量",
    cards: "暗流交易卡",
    candidates: "持久候选",
    darkflow: "暗流回测",
    shadow: "旧影子盘",
  };
  return (
    <div className="alert">
      <AlertTriangle size={18} />
      <div>
        <strong>部分模块加载失败</strong>
        <span>{Object.entries(errors).map(([key, value]) => `${labels[key as keyof LoadState]}: ${value}`).join("；")}</span>
      </div>
    </div>
  );
}

function DecisionCardView({ card, featured = false }: { card: DecisionCard; featured?: boolean }) {
  const target = card.entry_plan.take_profit_levels[0];
  const blocked = card.risk_gate.status !== "shadow_candidate";
  return (
    <article className={`decisionCard ${featured ? "featured" : ""} ${card.direction === "short" ? "short" : "long"}`}>
      <div className="decisionHeader">
        <div>
          <strong>{card.symbol}</strong>
          <span>{card.interval} · {strategyText(card.strategy_id)}</span>
        </div>
        <StatusPill label={blocked ? "blocked" : "shadow"} tone={blocked ? "warn" : "good"} />
      </div>
      <div className="decisionMeta">
        <span>{directionText(card.direction)}</span>
        <span>{setupText(card.setup_type)}</span>
        <span>Q {fmt(card.scores.quality_score, 0)}</span>
        <span>RR {fmt(card.risk.rr_ratio, 2)}</span>
      </div>
      <div className="levels">
        <Level label="Entry" value={card.entry_plan.planned_entry} />
        <Level label="Stop" value={card.entry_plan.planned_stop} danger />
        <Level label={target?.label ?? "TP"} value={target?.price} />
      </div>
      <div className="evidenceRow">
        {card.supporting_signals.slice(0, 4).map((item) => <span key={item}>{signalText(item)}</span>)}
        {!card.supporting_signals.length && <span>等待确认信号</span>}
      </div>
      {card.risk_gate.blockers.length > 0 && (
        <div className="blockers">{card.risk_gate.blockers.map(blockerText).join(" / ")}</div>
      )}
    </article>
  );
}

function GateList({ card, quality }: { card?: DecisionCard; quality: DataQuality | null }) {
  const rows = [
    { label: "实盘", pass: false, detail: "live disabled" },
    { label: "纸面", pass: false, detail: "等待 v2 晋级" },
    { label: "防重绘", pass: false, detail: "审计未完成" },
    { label: "数据", pass: quality?.status === "ok", detail: qualityText(quality?.status) },
    { label: "候选", pass: Boolean(card && card.risk_gate.status === "shadow_candidate"), detail: card ? gateText(card.risk_gate.status) : "无候选" },
  ];
  return (
    <div className="gateList">
      {rows.map((row) => (
        <div className="gateRow" key={row.label}>
          <span className={row.pass ? "gateDot pass" : "gateDot block"} />
          <strong>{row.label}</strong>
          <small>{row.detail}</small>
        </div>
      ))}
      <p className="note">没有通过防重绘、持久化候选表和 v2 影子盘前，交易卡只用于研究。</p>
    </div>
  );
}

function QualityPanel({ darkflow, cards }: { darkflow: DarkflowBacktest | null; cards: DecisionCardReport | null }) {
  return (
    <div className="qualityPanel">
      <Metric title="Quality Win" value={pct(darkflow?.quality_stats?.win_rate)} detail="v2 only" tone="good" compact />
      <Metric title="Quality PF" value={fmt(darkflow?.quality_stats?.profit_factor, 2)} detail="research" tone="good" compact />
      <Metric title="候选剧本" value={`${fmt(darkflow?.candidate_playbook_count, 0)} / ${fmt(darkflow?.watchlist_playbook_count, 0)}`} detail="candidate / watch" tone="info" compact />
      <Metric title="交易卡" value={fmt(cards?.card_count, 0)} detail={lineageText(cards?.policy)} tone="info" compact />
      <div className="lineageBox">
        <ShieldCheck size={18} />
        <div>
          <strong>{lineageText(darkflow?.policy)}</strong>
          <span>旧 feature/shadow 结果只保留为 Legacy/Control，不进入主交易路径。</span>
        </div>
      </div>
    </div>
  );
}

function CandidateLifecycle({ candidates }: { candidates: TradeCandidate[] }) {
  if (!candidates.length) {
    return <Empty text="尚未物化暗流 v2 候选，先运行 darkflow-trade-candidates 或后台任务" />;
  }
  return (
    <div className="candidateTable">
      <div className="candidateHead">
        <span>候选</span>
        <span>计划</span>
        <span>证据</span>
        <span>晋级</span>
      </div>
      {candidates.map((item) => (
        <div className="candidateRow" key={item.candidate_key}>
          <div>
            <strong>{item.symbol}</strong>
            <small>{directionText(item.direction)} · {strategyText(item.strategy_id)}</small>
          </div>
          <div>
            <span>{fmt(item.entry_price, 4)} / {fmt(item.stop_price, 4)} / {fmt(item.target_price, 4)}</span>
            <small>RR {fmt(item.rr_ratio, 2)} · Q {fmt(item.quality_score, 0)}</small>
          </div>
          <div>
            <StatusPill label={gateText(item.status)} tone={item.status === "shadow_candidate" ? "good" : "warn"} />
            <small>{item.supporting_signals.slice(0, 2).map(signalText).join(" / ") || "等待证据"}</small>
          </div>
          <div>
            <span>{promotionText(item.promotion_status)}</span>
            <small>anti-repaint: {auditText(item.anti_repaint_status)} · shadow: {shadowText(item.shadow_status)}</small>
          </div>
        </div>
      ))}
    </div>
  );
}

function Metric({ title, value, detail, tone = "info", compact = false }: { title: string; value: React.ReactNode; detail: string; tone?: string; compact?: boolean }) {
  return (
    <div className={`metric ${tone} ${compact ? "compact" : ""}`}>
      <span>{title}</span>
      <strong>{value ?? "--"}</strong>
      <small>{detail}</small>
    </div>
  );
}

function Panel({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panelHeader">
        <h2>{title}</h2>
        <span>{subtitle}</span>
      </div>
      {children}
    </section>
  );
}

function Level({ label, value, danger = false }: { label: string; value?: number; danger?: boolean }) {
  return (
    <div className="level">
      <span>{label}</span>
      <strong className={danger ? "danger" : ""}>{fmt(value, 4)}</strong>
    </div>
  );
}

function StatusPill({ label, tone }: { label: string; tone: string }) {
  return <span className={`statusPill ${tone}`}>{label}</span>;
}

function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function deriveRiskStatus(data: LoadState): { label: string; tone: string } {
  if (data.quality?.status === "error") return { label: "数据阻断", tone: "bad" };
  if (!data.cards?.cards.length) return { label: "研究等待", tone: "warn" };
  return { label: "研究模式", tone: "good" };
}

function fmt(value: unknown, digits = 2): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function pct(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return `${(value * 100).toFixed(1)}%`;
}

function modeText(value?: string) {
  return value === "paper_trading" ? "Paper" : value ?? "--";
}

function qualityText(value?: string) {
  return value === "ok" ? "正常" : value === "warning" ? "警告" : value === "error" ? "异常" : "未知";
}

function lineageText(policy?: Policy | null) {
  const lineage = policy?.lineage?.lineage;
  if (lineage === "core_darkflow_v2") return "核心暗流 v2";
  if (lineage === "legacy_feature_research") return "Legacy/Control";
  if (lineage === "legacy_baseline_v0") return "Legacy Baseline";
  return "未标记";
}

function directionText(value: string) {
  return value === "long" ? "做多" : value === "short" ? "做空" : value;
}

function strategyText(value: string) {
  return {
    pullback_to_cost: "成本回踩",
    liquidity_sweep_reversal: "扫损反转",
    breakout_confirmation: "突破确认",
    trend_ride_extension: "趋势延展",
  }[value] ?? value;
}

function setupText(value: string) {
  return {
    wick_pierce_reclaim: "刺破收回",
    first_touch: "首次触碰",
    body_break: "实体破位",
  }[value] ?? value;
}

function signalText(value: string) {
  return value.replace(/_/g, " ");
}

function blockerText(value: string) {
  return value.replace(/_/g, " ");
}

function gateText(value: string) {
  return value === "shadow_candidate" ? "影子候选" : value === "research_blocked" ? "研究阻断" : value;
}

function promotionText(value: string) {
  return value === "blocked" ? "阻断" : value === "shadow_ready_pending_audit" ? "待审计" : value === "shadow_running" ? "影子盘中" : value;
}

function auditText(value: string) {
  return value === "missing" ? "缺失" : value === "passed" ? "通过" : value;
}

function shadowText(value: string) {
  return value === "not_started" ? "未开始" : value === "collecting" ? "采集中" : value;
}

function timeText(value?: string | null) {
  if (!value) return "等待报告";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

createRoot(document.getElementById("root")!).render(<App />);
