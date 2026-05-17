import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AlertTriangle,
  BarChart3,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Clock3,
  Database,
  FileWarning,
  FlaskConical,
  Gauge,
  Layers3,
  LineChart,
  Lock,
  Menu,
  RefreshCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  TimerReset,
  X,
  XCircle,
} from "lucide-react";
import * as echarts from "echarts/core";
import { LineChart as EChartLineChart, RadarChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import "./styles.css";

echarts.use([RadarChart, EChartLineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const DARKFLOW_SHADOW_STRATEGY = "darkflow_v2_trade_candidate_shadow_forward_v1";

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

type EntryPlanStateReport = {
  candidate_count: number;
  generated_at?: string | null;
  freshness?: DarkflowFreshness;
  state_counts: Record<string, number>;
  reason_counts: Record<string, number>;
  missing_price_count: number;
  samples: EntryPlanSample[];
  thresholds: { entry_tolerance_pct: number };
  policy: Policy & { report_only?: boolean; mutates_candidate_state?: boolean };
};

type DarkflowFreshness = {
  status: "fresh" | "stale" | string;
  stale_reasons: string[];
  opportunity_status?: "active" | "quiet" | string;
  opportunity_reasons?: string[];
  latest_price_at?: string | null;
  latest_signal_snapshot_at?: string | null;
  latest_interaction_event_at?: string | null;
  latest_interaction_created_at?: string | null;
  latest_pipeline_run_at?: string | null;
  latest_candidate_setup_at?: string | null;
  latest_candidate_updated_at?: string | null;
  age_minutes?: Record<string, number | null>;
};

type EntryPlanSample = {
  candidate_key: string;
  symbol: string;
  direction: "long" | "short" | string;
  status: string;
  promotion_status: string;
  anti_repaint_status: string;
  shadow_status: string;
  entry_price: number;
  stop_price: number;
  target_price: number;
  quality_score: number;
  entry_plan_state: EntryPlanState;
};

type EntryPlanState = {
  state: string;
  reason: string;
  mark_price?: number | null;
  planned_entry?: number;
  planned_stop?: number;
  target_price?: number;
  invalidation_price?: number;
  entry_range?: { lower?: number; upper?: number; source?: string };
  valid_until?: string | null;
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
  decision_payload?: DecisionCard;
  shadow_stats?: {
    total_trades?: number;
    open_trades?: number;
    closed_trades?: number;
    win_rate?: number | null;
    profit_factor?: number | null;
    max_drawdown?: number | null;
  };
};

type RulebookReport = {
  indicator_count?: number;
  rules?: IndicatorRule[];
  official_to_internal?: Record<string, string[]>;
  policy?: Policy;
};

type IndicatorRule = {
  official_key: string;
  official_name: string;
  internal_keys: string[];
  family: string;
  primary_roles: string[];
  long_rule: string;
  short_rule: string;
  stop_rule: string;
  target_rule: string;
  blocker_rule: string;
  confirmation_required: string[];
  single_trigger_allowed: boolean;
  implementation_status: string;
};

type PlaybookCatalogReport = {
  playbook_count?: number;
  playbooks?: Playbook[];
  policy?: Policy;
};

type Playbook = {
  key: string;
  display_name: string;
  thesis: string;
  entry_indicators: string[];
  confirmation_indicators: string[];
  blocker_indicators: string[];
  target_indicators: string[];
  policy: string;
};

type IndicatorCoverageReport = {
  indicator_catalog?: IndicatorCatalogRow[];
  experiment_matrix?: IndicatorCatalogRow[];
  gaps?: unknown[];
};

type IndicatorCatalogRow = {
  key: string;
  hfd_name?: string;
  english_name?: string;
  family?: string;
  status?: string;
  role?: string;
  snapshot_count?: number;
  coverage_slots?: number;
  expected_coverage_slots?: number;
  coverage_pct?: number;
  covered_symbols?: string[];
  covered_timeframes?: string[];
  feature_event_count?: number;
  feature_labeled_count?: number;
  live_observation_count?: number;
  live_labeled_count?: number;
  used_in_live_strategy?: boolean;
  used_in_backtest?: boolean;
  used_for_opening_decisions?: boolean;
  required_for_scoring?: boolean;
  payload_status?: string;
  evidence_level?: string;
};

type ShadowTrade = {
  id: string;
  strategy_name: string;
  candidate_type: string;
  candidate_key: string;
  symbol: string;
  timeframe: string;
  direction: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  status: string;
  exit_price?: number | null;
  exit_reason?: string | null;
  pnl?: number | null;
  r_multiple?: number | null;
  mfe?: number | null;
  mae?: number | null;
  opened_at?: string | null;
  closed_at?: string | null;
};

type ShadowGroupStats = {
  symbol?: string;
  direction?: string;
  timeframe?: string;
  horizon?: string;
  candidate_key?: string;
  candidate_type?: string;
  open_trades?: number;
  closed_trades?: number;
  total_trades?: number;
  win_rate?: number | null;
  avg_pnl?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  promotion_status?: string;
  promotion_blockers?: string[];
};

type FrozenEntryPlan = {
  plan_type?: string;
  status?: string;
  state?: string;
  trigger?: string;
  planned_entry: number;
  planned_stop: number;
  take_profit_levels: Array<{ label: string; price: number; source: string }>;
  max_hold_bars: number;
  frozen_at?: string | null;
  valid_until?: string | null;
  entry_reference_price?: number;
  entry_range?: { lower?: number; upper?: number; source?: string };
  invalidation_price?: number;
  entry_tolerance_pct?: number;
  drift_limit_pct?: number;
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
  entry_plan: FrozenEntryPlan;
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

type BacktestResult = {
  strategy?: string;
  symbol?: string;
  coin?: string;
  interval?: string;
  timeframe?: string;
  trade_count?: number;
  win_rate?: number | null;
  avg_pnl_pct?: number | null;
  total_pnl_pct?: number | null;
  profit_factor?: number | null;
  max_drawdown_pct?: number | null;
  score?: number | null;
};

type BacktestsLatestReport = {
  id?: string;
  strategy?: string;
  status?: string;
  params?: Record<string, unknown>;
  results?: BacktestResult[];
  generated_at?: string | null;
};

type BacktestStats = {
  trade_count?: number;
  win_count?: number;
  loss_count?: number;
  win_rate?: number | null;
  avg_return?: number | null;
  median_return?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  avg_mfe?: number | null;
  avg_mae?: number | null;
};

type PlaybookBacktestItem = {
  key?: string;
  display_name?: string;
  sample_count?: number;
  confirmation_rate?: number | null;
  blocker_rate?: number | null;
  stats?: BacktestStats;
  confirmed_stats?: BacktestStats;
  top_segments?: Array<{ key?: string; symbol?: string; timeframe?: string; direction?: string; trade_count?: number; win_rate?: number | null; profit_factor?: number | null }>;
};

type PlaybookBacktestReport = {
  strategy_family?: string;
  horizon?: string;
  labeled_count?: number;
  covered_labeled_count?: number;
  candidate_playbook_count?: number;
  watchlist_playbook_count?: number;
  playbooks?: PlaybookBacktestItem[];
  policy?: Policy;
  generated_at?: string | null;
};

type PaperStatsGroup = {
  key?: string;
  total_trades?: number;
  open_trades?: number;
  closed_trades?: number;
  win_count?: number;
  loss_count?: number;
  win_rate?: number | null;
  avg_pnl?: number | null;
  total_pnl?: number | null;
  profit_factor?: number | null;
  max_drawdown?: number | null;
  avg_r_multiple?: number | null;
  open_mfe?: number | null;
  open_mae?: number | null;
};

type PaperStats = PaperStatsGroup & {
  gross_profit?: number | null;
  gross_loss?: number | null;
  best_trade?: number | null;
  worst_trade?: number | null;
  sample_ready?: boolean;
  sample_target?: number;
  minimum_sample?: number;
  sample_progress?: number | null;
  by_symbol?: PaperStatsGroup[];
};

type PaperTrade = {
  id: string;
  symbol: string;
  direction: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  status: string;
  pnl?: number | null;
  r_multiple?: number | null;
  mfe?: number | null;
  mae?: number | null;
  opened_at?: string | null;
  closed_at?: string | null;
};

type ExperimentIndicator = {
  key: string;
  hfd_name?: string;
  english_name?: string;
  family?: string;
  sample_count?: number;
  series_count?: number;
  unique_payload_shapes?: number;
  win_rate?: number | null;
  avg_return?: number | null;
  median_return?: number | null;
  profit_factor?: number | null;
  avg_mfe?: number | null;
  avg_mae?: number | null;
  long_count?: number;
  short_count?: number;
  status?: string;
  noise_risk?: string;
  recommendation?: string;
  used_for_execution_weights?: boolean;
  used_for_opening_decisions?: boolean;
};

type ExperimentEffectivenessReport = {
  horizon?: string;
  min_samples?: number;
  series_count?: number;
  event_count?: number;
  policy?: Policy;
  indicators?: ExperimentIndicator[];
  by_family?: Array<{ name?: string; indicator_count?: number; sample_count?: number; avg_return?: number | null; ready_count?: number }>;
};

type ResearchArmStats = {
  trade_count?: number;
  win_count?: number;
  loss_count?: number;
  win_rate?: number | null;
  win_rate_lower?: number | null;
  avg_return?: number | null;
  avg_return_lower?: number | null;
  avg_return_upper?: number | null;
  median_return?: number | null;
  profit_factor?: number | null;
  profit_factor_lower?: number | null;
  reliability_score?: number | null;
  avg_mfe?: number | null;
  avg_mae?: number | null;
  gross_win?: number;
  gross_loss?: number;
};

type ResearchEdgeStats = {
  avg_return_delta?: number | null;
  win_rate_delta?: number | null;
  profit_factor_delta?: number | null;
  candidate_trade_ratio?: number | null;
};

type FeaturePaperCandidate = {
  feature_key?: string;
  segment_key?: string;
  symbol?: string;
  timeframe?: string;
  direction?: string;
  sample_count?: number;
  win_rate?: number | null;
  profit_factor?: number | null;
  avg_return?: number | null;
  paper_ab_ready?: boolean;
  overfit_risk?: string;
  risk_reasons?: string[];
  pseudo_trade_metrics?: ResearchArmStats;
};

type FeaturePaperAbReport = {
  horizon?: string;
  selected_candidate_count?: number;
  selected_feature_keys?: string[];
  selected_segment_keys?: string[];
  data_quality?: { labeled_count?: number; candidate_pseudo_trade_count?: number; raw_candidate_pseudo_trade_count?: number; matched_control_pseudo_trade_count?: number; status?: string };
  quality?: Record<string, { effective_sample_count?: number; raw_sample_count?: number; dedupe_ratio?: number; unique_event_day_count?: number; unique_market_window_count?: number; overfit_risk?: string; risk_reasons?: string[] }>;
  arms?: {
    candidate?: ResearchArmStats;
    control?: ResearchArmStats;
    matched_control?: ResearchArmStats;
    all_control?: ResearchArmStats;
    edge?: ResearchEdgeStats;
    matched_edge?: ResearchEdgeStats;
    all_edge?: ResearchEdgeStats;
  };
  per_candidate?: FeaturePaperCandidate[];
  per_segment?: FeaturePaperCandidate[];
  candidate_screen?: { candidate_count?: number; watchlist_count?: number; rejected_summary?: Record<string, number> };
  segment_screen?: { candidate_count?: number; rejected_summary?: Record<string, number>; by_feature?: Array<{ feature_key?: string; count?: number }> };
  policy?: Policy;
  generated_at?: string | null;
  stale_seconds?: number | null;
};

type ShadowStats = {
  total_trades?: number;
  open_trades?: number;
  closed_trades?: number;
  win_rate?: number | null;
  profit_factor?: number | null;
  avg_pnl?: number | null;
  policy?: Policy;
  by_candidate?: ShadowGroupStats[];
  by_symbol?: ShadowGroupStats[];
  by_horizon?: ShadowGroupStats[];
};

type TradingSafety = {
  live_trading_enabled: boolean;
  kill_switch_active: boolean;
  manual_confirmation_required: boolean;
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
  entryStates: EntryPlanStateReport | null;
  darkflow: DarkflowBacktest | null;
  backtestsLatest: BacktestsLatestReport | null;
  playbookBacktest: PlaybookBacktestReport | null;
  paperStats: PaperStats | null;
  paperTrades: PaperTrade[] | null;
  shadow: ShadowStats | null;
  shadowTrades: ShadowTrade[] | null;
  rulebook: RulebookReport | null;
  playbooks: PlaybookCatalogReport | null;
  indicatorCoverage: IndicatorCoverageReport | null;
  experimentEffectiveness: ExperimentEffectivenessReport | null;
  featurePaperAb: FeaturePaperAbReport | null;
  featureSegmentPaperAb: FeaturePaperAbReport | null;
  safety: TradingSafety | null;
};

type SectionKey = keyof LoadState;
type LoadErrors = Partial<Record<SectionKey, string>>;
type PageId = "overview" | "tradeCards" | "candidates" | "entryPlans" | "experimentLab" | "backtest" | "paperTrading" | "shadow" | "indicatorMap" | "dataFreshness" | "safety" | "logs";
type NavItem = { id: PageId; label: string; icon: typeof Gauge; enabled: boolean; hint?: string };
type NavGroup = { title: string; items: NavItem[] };
type Filters = { query: string; direction: "all" | "long" | "short"; state: string; minQuality: number; onlyActionable: boolean };

const EMPTY_STATE: LoadState = {
  summary: null,
  quality: null,
  cards: null,
  candidates: null,
  entryStates: null,
  darkflow: null,
  backtestsLatest: null,
  playbookBacktest: null,
  paperStats: null,
  paperTrades: null,
  shadow: null,
  shadowTrades: null,
  rulebook: null,
  playbooks: null,
  indicatorCoverage: null,
  experimentEffectiveness: null,
  featurePaperAb: null,
  featureSegmentPaperAb: null,
  safety: null,
};

const PAGE_SIZE_OPTIONS = [10, 20, 50];
const QUICK_PAGES: PageId[] = ["overview", "tradeCards", "backtest", "paperTrading"];

const NAV_GROUPS: NavGroup[] = [
  {
    title: "总览",
    items: [
      { id: "overview", label: "指挥中心", icon: Gauge, enabled: true },
      { id: "dataFreshness", label: "系统健康", icon: CircleDot, enabled: true },
    ],
  },
  {
    title: "暗流交易",
    items: [
      { id: "tradeCards", label: "交易卡片", icon: Layers3, enabled: true },
      { id: "candidates", label: "候选池", icon: Search, enabled: true },
      { id: "entryPlans", label: "入场计划", icon: TimerReset, enabled: true },
    ],
  },
  {
    title: "信号闭环",
    items: [
      { id: "experimentLab", label: "实验室", icon: FlaskConical, enabled: true },
      { id: "backtest", label: "回测中心", icon: BarChart3, enabled: true },
      { id: "paperTrading", label: "纸上交易", icon: Database, enabled: true },
      { id: "shadow", label: "影子交易", icon: LineChart, enabled: true },
      { id: "indicatorMap", label: "指标教程映射", icon: BookOpen, enabled: true },
    ],
  },
  {
    title: "数据与运维",
    items: [
      { id: "safety", label: "安全开关", icon: ShieldCheck, enabled: true },
      { id: "logs", label: "日志与告警", icon: FileWarning, enabled: false, hint: "后续接入结构化日志" },
    ],
  },
];

async function fetchJson<T>(path: string, timeoutMs = 30000, attempts = 2): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, { signal: controller.signal });
      if (!response.ok) throw new Error(`${path}: ${response.status}`);
      return response.json();
    } catch (err) {
      lastError = err;
      if (attempt < attempts) await delay(700 * attempt);
    } finally {
      window.clearTimeout(timer);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

async function fetchFirstJson<T>(paths: string[], timeoutMs = 30000): Promise<T> {
  let lastError: unknown;
  for (const path of paths) {
    try {
      return await fetchJson<T>(path, timeoutMs, 2);
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function delay(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function App() {
  const [data, setData] = useState<LoadState>(EMPTY_STATE);
  const [errors, setErrors] = useState<LoadErrors>({});
  const [loading, setLoading] = useState(false);
  const [activePage, setActivePage] = useState<PageId>(() => pageFromHash());
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [filters, setFilters] = useState<Filters>({ query: "", direction: "all", state: "all", minQuality: 0, onlyActionable: false });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setErrors({});
    await Promise.all([
      loadSection("summary", () => fetchJson<Summary>("/system/summary")),
      loadSection("quality", () => fetchJson<DataQuality>("/data/quality-report")),
      loadSection("cards", () => fetchFirstJson<DecisionCardReport>(["/darkflow/decision-cards?limit=60", "/darkflow/decision-cards?limit=30"], 45000)),
      loadSection("candidates", () => fetchFirstJson<TradeCandidateReport>(["/darkflow/trade-candidates?limit=100", "/darkflow/trade-candidates?limit=50", "/darkflow/trade-candidates?limit=20"], 45000)),
      loadSection("entryStates", () => fetchFirstJson<EntryPlanStateReport>(["/darkflow/trade-candidates/entry-plan-states?limit=250", "/darkflow/trade-candidates/entry-plan-states?limit=100"], 45000)),
      loadSection("darkflow", () => fetchJson<DarkflowBacktest>("/darkflow/interactions/backtest/latest")),
      loadSection("safety", () => fetchJson<TradingSafety>("/trading/safety")),
    ]);
    setLoading(false);
  }

  async function loadSection<Key extends SectionKey>(key: Key, loader: () => Promise<LoadState[Key]>) {
    try {
      const value = await loader();
      setData((current) => ({ ...current, [key]: value }));
      setErrors((current) => {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      });
    } catch (err) {
      setErrors((current) => ({ ...current, [key]: errorText(err) }));
      setData((current) => current[key] === null ? current : current);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  useEffect(() => {
    const listener = () => setActivePage(pageFromHash());
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);

  useEffect(() => {
    if (activePage === "backtest") {
      if (data.backtestsLatest === null && !errors.backtestsLatest) void loadSection("backtestsLatest", () => fetchJson<BacktestsLatestReport>("/backtests/latest", 16000));
      if (data.playbookBacktest === null && !errors.playbookBacktest) void loadSection("playbookBacktest", () => fetchJson<PlaybookBacktestReport>("/darkflow/playbooks/backtest/latest?horizon=4h", 16000));
    }
    if (activePage === "paperTrading") {
      if (data.paperStats === null && !errors.paperStats) void loadSection("paperStats", () => fetchJson<PaperStats>("/paper/stats", 12000));
      if (data.paperTrades === null && !errors.paperTrades) void loadSection("paperTrades", () => fetchJson<PaperTrade[]>("/paper/trades?limit=80", 12000));
    }
    if (activePage === "shadow" && data.shadow === null && !errors.shadow) {
      void loadSection("shadow", () => fetchJson<ShadowStats>(`/shadow-paper/stats?strategy_name=${DARKFLOW_SHADOW_STRATEGY}`, 12000));
    }
    if (activePage === "shadow" && data.shadowTrades === null && !errors.shadowTrades) {
      void loadSection("shadowTrades", () => fetchJson<ShadowTrade[]>(`/shadow-paper/trades?limit=80&strategy_name=${DARKFLOW_SHADOW_STRATEGY}`, 12000));
    }
    if (activePage === "indicatorMap") {
      if (data.rulebook === null && !errors.rulebook) void loadSection("rulebook", () => fetchJson<RulebookReport>("/darkflow/rulebook", 12000));
      if (data.playbooks === null && !errors.playbooks) void loadSection("playbooks", () => fetchJson<PlaybookCatalogReport>("/darkflow/playbooks", 12000));
      if (data.indicatorCoverage === null && !errors.indicatorCoverage) void loadSection("indicatorCoverage", () => fetchJson<IndicatorCoverageReport>("/signals/experiments", 12000));
    }
    if (activePage === "experimentLab") {
      if (data.indicatorCoverage === null && !errors.indicatorCoverage) void loadSection("indicatorCoverage", () => fetchJson<IndicatorCoverageReport>("/signals/experiments", 16000));
      if (data.experimentEffectiveness === null && !errors.experimentEffectiveness) void loadSection("experimentEffectiveness", () => fetchJson<ExperimentEffectivenessReport>("/signals/experiment-effectiveness?horizon=4h&min_samples=5", 16000));
      if (data.featurePaperAb === null && !errors.featurePaperAb) void loadSection("featurePaperAb", () => fetchJson<FeaturePaperAbReport>("/features/paper-ab/latest?horizon=30m", 16000));
      if (data.featureSegmentPaperAb === null && !errors.featureSegmentPaperAb) void loadSection("featureSegmentPaperAb", () => fetchJson<FeaturePaperAbReport>("/features/segment-paper-ab/latest?horizon=30m", 16000));
    }
  }, [activePage, data.backtestsLatest, data.playbookBacktest, data.paperStats, data.paperTrades, data.shadow, data.shadowTrades, data.rulebook, data.playbooks, data.indicatorCoverage, data.experimentEffectiveness, data.featurePaperAb, data.featureSegmentPaperAb, errors.backtestsLatest, errors.playbookBacktest, errors.paperStats, errors.paperTrades, errors.shadow, errors.shadowTrades, errors.rulebook, errors.playbooks, errors.indicatorCoverage, errors.experimentEffectiveness, errors.featurePaperAb, errors.featureSegmentPaperAb]);

  const rows = useMemo(() => buildRows(data), [data]);
  const tradeRows = useMemo(() => rows.filter(hasTradeEvidence), [rows]);
  const filteredRows = useMemo(() => filterRows(tradeRows, filters), [tradeRows, filters]);
  const relevantErrors = useMemo(() => relevantLoadErrors(activePage, errors), [activePage, errors]);
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const visibleRows = filteredRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const selectedRow = filteredRows.find((item) => item.key === selectedKey) ?? filteredRows[0] ?? null;

  useEffect(() => {
    setPage(1);
  }, [filters, pageSize, activePage]);

  useEffect(() => {
    if (!selectedKey && filteredRows[0]) setSelectedKey(filteredRows[0].key);
    if (selectedKey && filteredRows.length && !filteredRows.some((item) => item.key === selectedKey)) setSelectedKey(filteredRows[0].key);
  }, [filteredRows, selectedKey]);

  function navigate(pageId: PageId) {
    setActivePage(pageId);
    window.location.hash = pageId;
    setMobileMenuOpen(false);
  }

  return (
    <main className="appShell">
      <Sidebar activePage={activePage} onNavigate={navigate} mobileOpen={mobileMenuOpen} onClose={() => setMobileMenuOpen(false)} />
      <section className="workspace">
        <MobileTopNav activePage={activePage} onNavigate={navigate} onMenu={() => setMobileMenuOpen(true)} />
        <TopStatusBar data={data} errors={relevantErrors} loading={loading} onRefresh={refresh} onJumpFreshness={() => navigate("dataFreshness")} />
        {Object.keys(relevantErrors).length > 0 && <LoadErrorBanner errors={relevantErrors} />}
        {activePage === "overview" && <OverviewPage data={data} rows={tradeRows} onNavigate={navigate} />}
        {activePage === "tradeCards" && (
          <TradeCardsPage
            rows={filteredRows}
            visibleRows={visibleRows}
            selectedRow={selectedRow}
            filters={filters}
            page={currentPage}
            pageSize={pageSize}
            total={filteredRows.length}
            loading={loading}
            errors={errors}
            onFilters={setFilters}
            onPage={setPage}
            onPageSize={setPageSize}
            onSelect={setSelectedKey}
            onNavigate={navigate}
          />
        )}
        {activePage === "candidates" && <CandidatePoolPage rows={tradeRows} onSelect={(key) => { setSelectedKey(key); navigate("tradeCards"); }} />}
        {activePage === "entryPlans" && <EntryPlansPage report={data.entryStates} rows={rows} onSelect={(key) => { setSelectedKey(key); navigate("tradeCards"); }} />}
        {activePage === "experimentLab" && <ExperimentLabPage data={data} rows={rows} onNavigate={navigate} />}
        {activePage === "backtest" && <BacktestPage data={data} />}
        {activePage === "paperTrading" && <PaperTradingPage stats={data.paperStats} trades={data.paperTrades} />}
        {activePage === "shadow" && <ShadowPage data={data} rows={rows} />}
        {activePage === "dataFreshness" && <DataFreshnessPage data={data} errors={errors} />}
        {activePage === "safety" && <SafetyPage safety={data.safety} />}
        {activePage === "indicatorMap" && <IndicatorMapPage data={data} rows={rows} />}
        {activePage === "logs" && <PlaceholderPage title="日志与告警" text="结构化日志和告警面板尚未接入。当前只在后端容器日志和运行记录中可查。" />}
      </section>
    </main>
  );
}

function Sidebar({ activePage, onNavigate, mobileOpen, onClose }: { activePage: PageId; onNavigate: (page: PageId) => void; mobileOpen: boolean; onClose: () => void }) {
  return (
    <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
      <div className="brandBlock">
        <div className="brandMark">HFD</div>
        <div>
          <strong>暗流交易台</strong>
          <span>研究模式 · 实盘关闭</span>
        </div>
        <button className="mobileClose" onClick={onClose} aria-label="关闭导航"><X size={18} /></button>
      </div>
      <nav className="sideNav">
        {NAV_GROUPS.map((group) => (
          <div className="navGroup" key={group.title}>
            <div className="navGroupTitle">{group.title}</div>
            {group.items.map((item) => <NavButton key={item.id} item={item} active={activePage === item.id} onNavigate={onNavigate} />)}
          </div>
        ))}
      </nav>
    </aside>
  );
}

function NavButton({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate: (page: PageId) => void }) {
  const Icon = item.icon;
  return (
    <button className={`navButton ${active ? "active" : ""} ${item.enabled ? "" : "disabled"}`} onClick={() => item.enabled && onNavigate(item.id)} title={item.enabled ? item.label : item.hint}>
      <Icon size={17} />
      <span>{item.label}</span>
      {!item.enabled && <small>待接入</small>}
    </button>
  );
}

function MobileTopNav({ activePage, onNavigate, onMenu }: { activePage: PageId; onNavigate: (page: PageId) => void; onMenu: () => void }) {
  return (
    <div className="mobileTopNav">
      <button className="iconOnly" onClick={onMenu} aria-label="打开导航"><Menu size={18} /></button>
      <div className="mobileQuickLinks">
        {QUICK_PAGES.map((id) => <button key={id} className={activePage === id ? "active" : ""} onClick={() => onNavigate(id)}>{pageLabel(id)}</button>)}
      </div>
    </div>
  );
}

function TopStatusBar({ data, errors, loading, onRefresh, onJumpFreshness }: { data: LoadState; errors: LoadErrors; loading: boolean; onRefresh: () => void; onJumpFreshness: () => void }) {
  const freshness = data.entryStates?.freshness;
  const stale = freshness?.status !== "fresh";
  return (
    <header className="topbar">
      <div className="titleBlock">
        <p>暗流系统预览版</p>
        <h1>{pageTitleFromHash()}</h1>
      </div>
      <div className="statusRail">
        <button className="statusToken clickable" onClick={onJumpFreshness} title="查看数据新鲜度">
          <span className={`statusDot ${stale ? "stale pulse" : "fresh"}`} />
          <span>暗流链路</span>
          <strong>{stale ? "滞后" : "新鲜"}</strong>
        </button>
        <div className="statusToken">
          <Clock3 size={15} />
          <span>价格</span>
          <strong>{ageText(freshness?.age_minutes?.price)}</strong>
        </div>
        <div className="statusToken locked">
          <Lock size={15} />
          <span>实盘</span>
          <strong>{data.safety?.live_trading_enabled ? "开启" : "关闭"}</strong>
        </div>
        <div className="statusToken locked">
          <ShieldCheck size={15} />
          <span>影子</span>
          <strong>隔离</strong>
        </div>
        <button className="refreshButton" onClick={onRefresh} disabled={loading}>
          <RefreshCcw size={16} />
          {loading ? "刷新中" : "刷新"}
        </button>
      </div>
      {Object.keys(errors).length > 0 && <span className="topWarning">部分数据加载失败</span>}
    </header>
  );
}

function OverviewPage({ data, rows, onNavigate }: { data: LoadState; rows: CardRow[]; onNavigate: (page: PageId) => void }) {
  const waiting = rows.filter((item) => item.state === "waiting").length;
  const shadowReady = rows.filter((item) => item.promotionStatus === "shadow_forward_pending" || item.status === "shadow_candidate").length;
  return (
    <div className="pageStack">
      <section className="metricGrid">
        <Metric title="暗流质量样本" value={`${fmt(data.darkflow?.quality_interaction_count, 0)} / ${fmt(data.darkflow?.interaction_count, 0)}`} detail={`质量样本盈利因子 ${fmt(data.darkflow?.quality_stats?.profit_factor, 2)}`} tone="good" />
        <Metric title="候选总数" value={fmt(rows.length, 0)} detail={`${shadowReady} 个进入影子观察边界`} tone="info" />
        <Metric title="等待入场" value={fmt(waiting, 0)} detail="价格尚未进入冻结入场区间" tone="warn" />
        <Metric title="数据质量" value={qualityText(data.quality?.status)} detail={`${data.quality?.issues.length ?? 0} 个问题`} tone={data.quality?.status === "ok" ? "good" : "warn"} />
      </section>
      <section className="twoColumn">
        <Panel title="下一步最该看什么" subtitle="按交易员阅读顺序组织">
          <div className="actionList">
            <ActionItem icon={Layers3} title="先看交易卡片" text="查看每个信号的入场区间、风控审计、阻断理由和雷达图。" action="打开交易卡片" onClick={() => onNavigate("tradeCards")} />
            <ActionItem icon={TimerReset} title="再看入场计划" text="区分等待、错过、过期和作废，确认可开仓点位是否仍有效。" action="查看入场计划" onClick={() => onNavigate("entryPlans")} />
            <ActionItem icon={FlaskConical} title="追踪信号闭环" text="实验室、回测、纸上和影子交易决定一个指标能不能真正进入评分。" action="打开实验室" onClick={() => onNavigate("experimentLab")} />
            <ActionItem icon={CircleDot} title="最后看数据新鲜度" text="判断当前没有机会是市场安静，还是采集和候选管道滞后。" action="查看新鲜度" onClick={() => onNavigate("dataFreshness")} />
          </div>
        </Panel>
        <Panel title="实盘边界" subtitle="当前不会发真实订单">
          <SafetySummary safety={data.safety} />
        </Panel>
      </section>
    </div>
  );
}

function TradeCardsPage(props: {
  rows: CardRow[];
  visibleRows: CardRow[];
  selectedRow: CardRow | null;
  filters: Filters;
  page: number;
  pageSize: number;
  total: number;
  loading: boolean;
  errors: LoadErrors;
  onFilters: (value: Filters) => void;
  onPage: (value: number) => void;
  onPageSize: (value: number) => void;
  onSelect: (key: string) => void;
  onNavigate: (page: PageId) => void;
}) {
  const { rows, visibleRows, selectedRow, filters, page, pageSize, total, loading, errors, onFilters, onPage, onPageSize, onSelect, onNavigate } = props;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="pageStack">
      <TradeFilters filters={filters} onChange={onFilters} />
      <section className="tradeLayout">
        <div className="cardListPane">
          <div className="listHeader">
            <div>
              <h2>交易卡片</h2>
              <span>每张卡只展示交易所需的核心判断，详细理由在右侧。</span>
            </div>
            <strong>{total} 条</strong>
          </div>
          {loading && !rows.length && <StateBox type="loading" title="正在加载交易卡片" text="正在读取暗流候选、入场计划和风控审计。" />}
          {!rows.length && errors.candidates && <StateBox type="error" title="交易候选加载失败" text={errors.candidates} />}
          {!rows.length && !errors.candidates && errors.cards && <StateBox type="error" title="决策卡补充数据加载失败" text="候选主接口尚未返回，决策卡接口也失败。请稍后刷新。" />}
          {rows.length > 0 && errors.cards && <StateBox type="error" title="部分决策卡补充数据失败" text="候选卡片已显示，部分来源决策卡暂时缺失，不影响阅读当前入场计划。" />}
          {!loading && !errors.candidates && total === 0 && <StateBox type="empty" title="当前无符合条件的暗流候选" text="可以清空筛选条件，或前往策略研究查看是否样本不足。" action="查看策略研究" onAction={() => onNavigate("backtest")} />}
          <div className="tradeCardList">
            {visibleRows.map((row) => <TradeCardItem key={row.key} row={row} active={selectedRow?.key === row.key} onClick={() => onSelect(row.key)} />)}
          </div>
          <Pagination page={page} pageSize={pageSize} total={total} totalPages={totalPages} onPage={onPage} onPageSize={onPageSize} />
        </div>
        <TradeDetailPane row={selectedRow} />
      </section>
    </div>
  );
}

function TradeFilters({ filters, onChange }: { filters: Filters; onChange: (value: Filters) => void }) {
  return (
    <section className="filterBar">
      <div className="filterSearch">
        <Search size={16} />
        <input value={filters.query} onChange={(event) => onChange({ ...filters, query: event.target.value })} placeholder="搜索币种或策略，例如 BTC / HYPE / 成本回踩" />
      </div>
      <SegmentedControl label="方向" value={filters.direction} options={[{ label: "全部", value: "all" }, { label: "做多", value: "long" }, { label: "做空", value: "short" }]} onChange={(value) => onChange({ ...filters, direction: value as Filters["direction"] })} />
      <label className="selectLabel">
        <span>状态</span>
        <select value={filters.state} onChange={(event) => onChange({ ...filters, state: event.target.value })}>
          <option value="all">全部状态</option>
          <option value="waiting">等待入场</option>
          <option value="missed">已错过</option>
          <option value="expired">时间过期</option>
          <option value="invalidated">条件作废</option>
          <option value="blocked">研究阻断</option>
          <option value="shadow_candidate">影子候选</option>
        </select>
      </label>
      <label className="rangeLabel">
        <span>最低评分 {filters.minQuality}</span>
        <input type="range" min="0" max="100" step="5" value={filters.minQuality} onChange={(event) => onChange({ ...filters, minQuality: Number(event.target.value) })} />
      </label>
      <label className="checkLabel">
        <input type="checkbox" checked={filters.onlyActionable} onChange={(event) => onChange({ ...filters, onlyActionable: event.target.checked })} />
        <span>只看仍可观察入场</span>
      </label>
    </section>
  );
}

function TradeCardItem({ row, active, onClick }: { row: CardRow; active: boolean; onClick: () => void }) {
  return (
    <button className={`tradeCard ${active ? "active" : ""} ${row.direction}`} onClick={onClick}>
      <div className="tradeCardTop">
        <div>
          <strong>{row.symbol}</strong>
          <span>{directionText(row.direction)} · {timeframeText(row.interval)} · {strategyText(row.strategyId)}</span>
        </div>
        <div className="cardBadges">
          {row.duplicateCount > 1 && <span className="mergeBadge">合并 {row.duplicateCount}</span>}
          <StatusBadge tone={stateTone(row.state)} label={entryStateText(row.state)} />
        </div>
      </div>
      <div className="scoreStrip">
        <ScoreCell label="评分" value={fmt(row.qualityScore, 0)} />
        <ScoreCell label="盈亏比" value={fmt(row.rrRatio, 2)} />
        <ScoreCell label="审计" value={auditText(row.auditStatus)} />
      </div>
      <div className="pricePlanMini">
        <LevelLine label="入场区间" value={row.entryRangeText} />
        <LevelLine label="止损" value={fmt(row.stopPrice, 4)} danger />
        <LevelLine label="目标" value={fmt(row.targetPrice, 4)} />
      </div>
      <div className="reasonPreview">
        <span>{row.primaryReason}</span>
        <small>{row.secondaryReason}</small>
      </div>
    </button>
  );
}

function TradeDetailPane({ row }: { row: CardRow | null }) {
  if (!row) {
    return <aside className="detailPane"><StateBox type="empty" title="请选择一张交易卡片" text="左侧点击卡片后，这里会显示完整入场逻辑、风控审计和评分雷达图。" /></aside>;
  }
  return (
    <aside className="detailPane">
      <div className="detailHeader">
        <div>
          <p>{strategyText(row.strategyId)}</p>
          <h2>{row.symbol} · {directionText(row.direction)}</h2>
        </div>
        <StatusBadge tone={stateTone(row.state)} label={entryStateText(row.state)} />
      </div>
      <section className="detailSection conclusion">
        <h3>交易结论</h3>
        <p>{tradeConclusion(row)}</p>
        {row.duplicateCount > 1 && <div className="mergeNotice"><strong>同计划已折叠：</strong>{row.variantSummary}</div>}
        <div className="auditGrid">
          <AuditItem label="纸上交易" ok={row.paperEligible} text={row.paperEligible ? "允许" : "未允许"} />
          <AuditItem label="实盘交易" ok={row.liveEligible} text={row.liveEligible ? "允许" : "关闭"} />
          <AuditItem label="防重绘" ok={row.auditStatus === "passed"} text={auditText(row.auditStatus)} />
          <AuditItem label="影子样本" ok={row.shadowStatus === "collecting"} text={shadowText(row.shadowStatus)} />
        </div>
      </section>
      <section className="detailSection">
        <h3>价格计划</h3>
        <div className="priceGrid">
          <Level label="当前价" value={row.markPrice} />
          <Level label="计划入场" value={row.entryPrice} />
          <Level label="止损" value={row.stopPrice} danger />
          <Level label="第一目标" value={row.targetPrice} />
          <Level label="失效价" value={row.invalidationPrice} danger />
          <Level label="盈亏比" value={row.rrRatio} />
        </div>
        <div className="textLine"><strong>冻结入场区间：</strong>{row.entryRangeText}</div>
        <div className="textLine"><strong>有效期：</strong>{timeText(row.validUntil)}</div>
      </section>
      <section className="detailSection twoPane">
        <div>
          <h3>信号理由</h3>
          <ReasonList title="支持理由" items={row.supportingSignals.map(signalDetail)} empty="暂无支持信号，等待更多确认。" />
          <ReasonList title="阻断风险" items={[...row.blockers, ...row.promotionBlockers].map(blockerDetail)} empty="暂无阻断风险。" warn />
        </div>
        <div>
          <h3>多维评分</h3>
          <RadarScore row={row} />
          <p className="chartNote">浅色基准代表理想通过信号，绿色代表当前信号。</p>
        </div>
      </section>
      <section className="detailSection">
        <h3>原始状态码</h3>
        <div className="codeGrid">
          <CodeItem label="入场状态" value={row.stateRaw} />
          <CodeItem label="原因" value={row.reasonRaw} />
          <CodeItem label="晋级状态" value={row.promotionStatus} />
          <CodeItem label="候选键" value={shortKey(row.key)} />
        </div>
        {row.duplicateCount > 1 && <div className="sourceKeys"><strong>同计划来源：</strong>{row.duplicateKeys.slice(0, 8).map((key) => <code key={key}>{shortKey(key)}</code>)}{row.duplicateKeys.length > 8 && <span>还有 {row.duplicateKeys.length - 8} 条</span>}</div>}
      </section>
    </aside>
  );
}

function CandidatePoolPage({ rows, onSelect }: { rows: CardRow[]; onSelect: (key: string) => void }) {
  const grouped = countBy(rows, (row) => row.status);
  const groups = candidateGroups(rows);
  const duplicatePlans = groups.reduce((total, group) => total + Math.max(0, group.rows.reduce((sum, row) => sum + row.duplicateCount, 0) - group.rows.length), 0);
  return (
    <div className="pageStack">
      <section className="metricGrid compactMetrics">
        <Metric title="候选组数" value={fmt(groups.length, 0)} detail={`${fmt(rows.length, 0)} 张折叠后交易卡`} tone="info" />
        <Metric title="影子候选" value={fmt(grouped.shadow_candidate ?? 0, 0)} detail="仍需影子样本积累" tone="good" />
        <Metric title="研究阻断" value={fmt(grouped.research_blocked ?? 0, 0)} detail="防重绘或样本未通过" tone="warn" />
        <Metric title="已折叠重复" value={fmt(duplicatePlans, 0)} detail="同入场/止损/目标计划不再刷屏" tone="info" />
      </section>
      <Panel title="候选池深度" subtitle="按币种、方向、策略聚合，先看哪一类候选最值得研究">
        <div className="candidateGroupList">
          {groups.map((group) => (
            <button className="candidateGroup" key={group.key} onClick={() => onSelect(group.best.key)}>
              <div>
                <strong>{group.symbol}</strong>
                <span>{directionText(group.direction)} · {strategyText(group.strategyId)}</span>
              </div>
              <div className="groupStats">
                <small>计划 {group.rows.length} 组</small>
                <small>最高评分 {fmt(group.maxQuality, 0)}</small>
                <small>最好 RR {fmt(group.maxRr, 2)}</small>
                <small>{group.latestState}</small>
              </div>
              <p>{group.reason}</p>
            </button>
          ))}
          {!rows.length && <StateBox type="empty" title="候选池为空" text="当前没有已物化的暗流候选，等待 darkflow-worker 产出。" />}
        </div>
      </Panel>
    </div>
  );
}

function EntryPlansPage({ report, rows, onSelect }: { report: EntryPlanStateReport | null; rows: CardRow[]; onSelect: (key: string) => void }) {
  const states = ["waiting", "missed", "expired", "invalidated", "triggered", "missing_price", "invalid_shape"];
  return (
    <div className="pageStack">
      <Panel title="状态术语" subtitle="全界面统一解释">
        <div className="definitionGrid">
          <Definition title="等待" code="waiting" text="价格还没有进入冻结入场区间，仍可继续观察。" />
          <Definition title="错过" code="missed" text="价格已经越过入场区间，当前不再追价。" />
          <Definition title="过期" code="expired" text="超过有效期，计划按时间失效。" />
          <Definition title="作废" code="invalidated" text="触发止损或结构破坏条件，计划被条件否定。" />
        </div>
      </Panel>
      <section className="stateBuckets wide">
        {states.map((state) => <div className={`stateBucket ${stateTone(state)}`} key={state}><span>{entryStateText(state)}</span><strong>{fmt(report?.state_counts[state] ?? 0, 0)}</strong><small>{stateHint(state)}</small></div>)}
      </section>
      <Panel title="入场计划样本" subtitle={`更新时间 ${timeText(report?.generated_at)}`}>
        <div className="tableList">
          {rows.slice(0, 100).map((row) => (
            <button className="tableRow" key={row.key} onClick={() => onSelect(row.key)}>
              <strong>{row.symbol}</strong>
              <span>{entryStateText(row.state)} · {stateReasonText(row.reasonRaw)}</span>
              <span>当前价 {fmt(row.markPrice, 4)}</span>
              <span>{row.entryRangeText}</span>
            </button>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ShadowPage({ data, rows }: { data: LoadState; rows: CardRow[] }) {
  const equity = shadowEquitySeries(data.shadowTrades ?? []);
  const topGroups = (data.shadow?.by_candidate ?? []).slice(0, 10);
  return (
    <div className="pageStack">
      <section className="metricGrid compactMetrics">
        <Metric title="影子总交易" value={fmt(data.shadow?.total_trades, 0)} detail="隔离表 shadow_paper_trades" tone="info" />
        <Metric title="影子胜率" value={pct(data.shadow?.win_rate)} detail="只用于研究观察" tone="good" />
        <Metric title="影子盈利因子" value={fmt(data.shadow?.profit_factor, 2)} detail="未进入实盘" tone="good" />
        <Metric title="等待影子样本" value={fmt(rows.filter((row) => row.shadowStatus === "not_started").length, 0)} detail="价格未触发或样本未开" tone="warn" />
      </section>
      <Panel title="影子权益曲线" subtitle="根据最近影子交易的已平仓盈亏重建，不代表实盘账户">
        {equity.length > 1 ? <EquityChart points={equity} /> : <StateBox type="empty" title="权益曲线样本不足" text="当前影子交易还没有足够的已平仓记录，先展示聚合统计。" />}
      </Panel>
      <Panel title="候选策略表现" subtitle="按候选来源聚合，优先看样本数、盈利因子和回撤">
        <div className="tableList">
          {topGroups.map((item) => (
            <div className="tableRow shadowRow" key={`${item.candidate_key}-${item.horizon}`}>
              <strong>{item.symbol || "--"}</strong>
              <span>{directionText(item.direction || "")} · {timeframeText(item.horizon || item.timeframe || "")}</span>
              <span>样本 {fmt(item.closed_trades, 0)} · 胜率 {pct(item.win_rate)}</span>
              <span>PF {fmt(item.profit_factor, 2)} · 回撤 {pct(item.max_drawdown)}</span>
            </div>
          ))}
          {!topGroups.length && <StateBox type="empty" title="暂无候选聚合" text="影子交易统计接口尚未返回候选维度数据。" />}
        </div>
      </Panel>
      <Panel title="影子隔离说明" subtitle="不会触发真实订单">
        <p className="bodyText">影子纸上交易只写入隔离研究表，不会触发真实纸上交易，也不会触发实盘订单。这里用于观察候选策略是否持续积累样本，以及样本表现是否稳定。</p>
      </Panel>
    </div>
  );
}

function ExperimentLabPage({ data, rows, onNavigate }: { data: LoadState; rows: CardRow[]; onNavigate: (page: PageId) => void }) {
  const effectRows = [...(data.experimentEffectiveness?.indicators ?? [])]
    .sort((a, b) => Number(b.sample_count ?? 0) - Number(a.sample_count ?? 0))
    .slice(0, 12);
  const featureAb = data.featurePaperAb;
  const segmentAb = data.featureSegmentPaperAb;
  const candidateArm = segmentAb?.arms?.candidate ?? featureAb?.arms?.candidate;
  const controlArm = segmentAb?.arms?.matched_control ?? segmentAb?.arms?.all_control ?? featureAb?.arms?.control;
  const selectedCount = Number(segmentAb?.selected_candidate_count ?? featureAb?.selected_candidate_count ?? 0);
  const labeledCount = Number(segmentAb?.data_quality?.labeled_count ?? featureAb?.data_quality?.labeled_count ?? data.experimentEffectiveness?.event_count ?? 0);
  const deepUsed = indicatorMapRows(data, rows).filter((item) => item.stage === "候选/影子" || item.stage === "已进评分" || item.stage === "回测覆盖").length;
  return (
    <div className="pageStack">
      <Panel title="信号闭环说明" subtitle="指标不能只看一次回测，必须经过实验、回测、影子/纸上前向验证">
        <div className="loopSteps">
          <div><strong>1. 指标实验</strong><span>先验证暗流指标本身是否有稳定样本、胜率和盈利因子。</span></div>
          <div><strong>2. 回测筛选</strong><span>用暗流教程剧本和交互样本过滤掉噪声形态。</span></div>
          <div><strong>3. 影子前向</strong><span>隔离运行候选策略，积累不影响真实纸上的前向样本。</span></div>
          <div><strong>4. 纸上晋级</strong><span>只有前向结果足够稳定，才允许进入真实纸上交易观察。</span></div>
        </div>
      </Panel>
      <section className="metricGrid compactMetrics">
        <Metric title="实验样本" value={fmt(labeledCount, 0)} detail={`4h 实验事件 ${fmt(data.experimentEffectiveness?.event_count, 0)}`} tone="info" />
        <Metric title="分段候选" value={fmt(selectedCount, 0)} detail={`30m A/B：${researchStatusText(segmentAb?.data_quality?.status ?? featureAb?.data_quality?.status)}`} tone={selectedCount > 0 ? "good" : "warn"} />
        <Metric title="候选胜率" value={pct(candidateArm?.win_rate)} detail={`对照胜率 ${pct(controlArm?.win_rate)}`} tone="good" />
        <Metric title="候选盈利因子" value={fmt(candidateArm?.profit_factor, 2)} detail={`对照 PF ${fmt(controlArm?.profit_factor, 2)}`} tone="good" />
      </section>
      <section className="twoColumn">
        <Panel title="分段纸上 A/B" subtitle={`样本去重后的候选表现，更新时间 ${timeText(segmentAb?.generated_at)}`}>
          <div className="abCompareGrid">
            <ResearchArm title="候选组" stats={candidateArm} />
            <ResearchArm title="对照组" stats={controlArm} />
          </div>
          <div className="researchNote">
            <strong>当前结论：</strong>{segmentAbConclusion(segmentAb)}
          </div>
        </Panel>
        <Panel title="闭环缺口" subtitle="这些数字决定下一步该补什么">
          <div className="auditGrid labAudit">
            <AuditItem label="指标深度使用" ok={deepUsed > 0} text={`${deepUsed} 个`} />
            <AuditItem label="候选 A/B" ok={selectedCount > 0} text={selectedCount > 0 ? "已有候选" : "暂无候选"} />
            <AuditItem label="影子交易" ok={Number(data.shadow?.closed_trades ?? 0) > 0} text={`${fmt(data.shadow?.closed_trades, 0)} 已平仓`} />
            <AuditItem label="纸上样本" ok={Boolean(data.paperStats?.sample_ready)} text={`${fmt(data.paperStats?.closed_trades, 0)} / ${fmt(data.paperStats?.minimum_sample, 0)}`} />
          </div>
          <div className="actionList compactActions">
            <ActionItem icon={BarChart3} title="看回测中心" text="确认暗流剧本和批量策略是不是仍有正收益。" action="打开回测" onClick={() => onNavigate("backtest")} />
            <ActionItem icon={LineChart} title="看影子交易" text="优先积累候选前向样本，不污染真实纸上统计。" action="打开影子" onClick={() => onNavigate("shadow")} />
            <ActionItem icon={Database} title="看纸上交易" text="真实纸上结果才是实盘前最接近的前向验证。" action="打开纸上" onClick={() => onNavigate("paperTrading")} />
          </div>
        </Panel>
      </section>
      <Panel title="暗流指标实验表现" subtitle="只展示研究证据，不代表已经用于开仓评分">
        <div className="indicatorMapList">
          {effectRows.map((item) => (
            <div className="indicatorRow labIndicator" key={item.key}>
              <div>
                <strong>{indicatorName(item.key)}</strong>
                <span>{item.key} · {familyText(item.family)}</span>
              </div>
              <StatusBadge tone={experimentTone(item.status)} label={experimentStatusText(item.status)} />
              <p>{experimentRecommendationText(item)}</p>
              <div className="indicatorStats">
                <small>样本 {fmt(item.sample_count, 0)}</small>
                <small>胜率 {pct(item.win_rate)}</small>
                <small>PF {fmt(item.profit_factor, 2)}</small>
                <small>开仓权重 {item.used_for_opening_decisions ? "已用" : "未用"}</small>
              </div>
            </div>
          ))}
          {!effectRows.length && <StateBox type="empty" title="实验室数据未加载" text="等待 /signals/experiment-effectiveness 或 /signals/experiments 返回。" />}
        </div>
      </Panel>
    </div>
  );
}

function BacktestPage({ data }: { data: LoadState }) {
  const darkflow = data.darkflow;
  const batchRows = [...(data.backtestsLatest?.results ?? [])]
    .sort((a, b) => Number(b.score ?? b.profit_factor ?? 0) - Number(a.score ?? a.profit_factor ?? 0))
    .slice(0, 10);
  const playbookRows = [...(data.playbookBacktest?.playbooks ?? [])]
    .sort((a, b) => Number(b.confirmed_stats?.profit_factor ?? b.stats?.profit_factor ?? 0) - Number(a.confirmed_stats?.profit_factor ?? a.stats?.profit_factor ?? 0))
    .slice(0, 8);
  return (
    <div className="pageStack">
      <section className="metricGrid compactMetrics">
        <Metric title="质量样本胜率" value={pct(darkflow?.quality_stats?.win_rate)} detail="已过滤质量门槛" tone="good" />
        <Metric title="质量样本盈利因子" value={fmt(darkflow?.quality_stats?.profit_factor, 2)} detail="研究指标，不等于实盘" tone="good" />
        <Metric title="最大回撤" value={pct(darkflow?.quality_stats?.max_drawdown)} detail="回测样本口径" tone="warn" />
        <Metric title="候选剧本" value={`${fmt(data.playbookBacktest?.candidate_playbook_count ?? darkflow?.candidate_playbook_count, 0)} / ${fmt(data.playbookBacktest?.watchlist_playbook_count ?? darkflow?.watchlist_playbook_count, 0)}`} detail="候选 / 观察" tone="info" />
      </section>
      <section className="twoColumn">
        <Panel title="暗流教程剧本回测" subtitle={`覆盖标注 ${fmt(data.playbookBacktest?.covered_labeled_count, 0)} / ${fmt(data.playbookBacktest?.labeled_count, 0)}`}>
          <div className="tableList">
            {playbookRows.map((item) => {
              const stats = item.confirmed_stats ?? item.stats;
              return (
                <div className="tableRow researchRow" key={item.key || item.display_name}>
                  <strong>{playbookText(item.key || item.display_name || "--")}</strong>
                  <span>样本 {fmt(item.sample_count ?? stats?.trade_count, 0)} · 确认 {pct(item.confirmation_rate)}</span>
                  <span>胜率 {pct(stats?.win_rate)} · PF {fmt(stats?.profit_factor, 2)}</span>
                  <span>回撤 {pct(stats?.max_drawdown)} · 阻断 {pct(item.blocker_rate)}</span>
                </div>
              );
            })}
            {!playbookRows.length && <StateBox type="empty" title="剧本回测未加载" text="等待 /darkflow/playbooks/backtest/latest 返回。" />}
          </div>
        </Panel>
        <Panel title="批量策略回测排行" subtitle={`最新任务 ${data.backtestsLatest?.status || "等待数据"}`}>
          <div className="tableList">
            {batchRows.map((item, index) => (
              <div className="tableRow researchRow" key={`${item.symbol}-${item.interval}-${index}`}>
                <strong>{item.symbol || item.coin || "--"}</strong>
                <span>{timeframeText(item.interval || item.timeframe || "")} · 样本 {fmt(item.trade_count, 0)}</span>
                <span>收益 {pct(item.total_pnl_pct)} · 胜率 {pct(item.win_rate)}</span>
                <span>PF {fmt(item.profit_factor, 2)} · 回撤 {pct(item.max_drawdown_pct)}</span>
              </div>
            ))}
            {!batchRows.length && <StateBox type="empty" title="批量回测未加载" text="等待 /backtests/latest 返回。" />}
          </div>
        </Panel>
      </section>
      <Panel title="回测解释" subtitle={timeText(darkflow?.generated_at)}>
        <p className="bodyText">回测只负责初筛策略和暗流形态，不能直接代表可开仓。真正提高胜率和盈亏比，要看同一批候选能否继续通过实验室 A/B、影子前向样本和真实纸上交易。</p>
      </Panel>
    </div>
  );
}

function PaperTradingPage({ stats, trades }: { stats: PaperStats | null; trades: PaperTrade[] | null }) {
  const equity = paperEquitySeries(trades ?? []);
  const bySymbol = [...(stats?.by_symbol ?? [])]
    .sort((a, b) => Number(b.closed_trades ?? 0) - Number(a.closed_trades ?? 0))
    .slice(0, 8);
  return (
    <div className="pageStack">
      <section className="metricGrid compactMetrics">
        <Metric title="纸上交易" value={fmt(stats?.total_trades, 0)} detail={`开仓 ${fmt(stats?.open_trades, 0)} · 平仓 ${fmt(stats?.closed_trades, 0)}`} tone="info" />
        <Metric title="纸上胜率" value={pct(stats?.win_rate)} detail={`样本进度 ${pct(stats?.sample_progress)}`} tone={Number(stats?.win_rate ?? 0) >= 0.5 ? "good" : "warn"} />
        <Metric title="纸上盈利因子" value={fmt(stats?.profit_factor, 2)} detail={`目标样本 ${fmt(stats?.minimum_sample, 0)}-${fmt(stats?.sample_target, 0)}`} tone={Number(stats?.profit_factor ?? 0) >= 1.2 ? "good" : "warn"} />
        <Metric title="最大回撤" value={pct(stats?.max_drawdown)} detail={`平均 R ${fmt(stats?.avg_r_multiple, 2)}`} tone="warn" />
      </section>
      {!stats?.sample_ready && <StateBox type="empty" title="纸上样本还不够定结论" text={`当前已平仓 ${fmt(stats?.closed_trades, 0)}，至少需要 ${fmt(stats?.minimum_sample, 0)} 条前向样本后，胜率和盈利因子才更可信。`} />}
      <section className="twoColumn">
        <Panel title="纸上权益曲线" subtitle="按已平仓纸上交易重建，仅用于前向验证">
          {equity.length > 1 ? <EquityChart points={equity} /> : <StateBox type="empty" title="权益曲线样本不足" text="等待更多纸上交易平仓后显示趋势。" />}
        </Panel>
        <Panel title="按币种表现" subtitle="用来判断某些币种是否拖累总体胜率">
          <div className="tableList">
            {bySymbol.map((item) => (
              <div className="tableRow compactRow" key={item.key}>
                <strong>{item.key || "--"}</strong>
                <span>平仓 {fmt(item.closed_trades, 0)} · 胜率 {pct(item.win_rate)}</span>
                <span>PF {fmt(item.profit_factor, 2)} · R {fmt(item.avg_r_multiple, 2)}</span>
                <span>回撤 {pct(item.max_drawdown)}</span>
              </div>
            ))}
            {!bySymbol.length && <StateBox type="empty" title="暂无币种分组" text="纸上统计接口尚未返回 by_symbol。" />}
          </div>
        </Panel>
      </section>
      <Panel title="最近纸上交易" subtitle="真实纸上前向验证，不等于实盘订单">
        <div className="tableList">
          {(trades ?? []).slice(0, 50).map((trade) => (
            <div className="tableRow paperTradeRow" key={trade.id}>
              <strong>{trade.symbol}</strong>
              <span>{directionText(trade.direction)} · {tradeStatusText(trade.status)} · {timeShort(trade.opened_at)}</span>
              <span>入场 {fmt(trade.entry_price, 4)} · 止损 {fmt(trade.stop_loss, 4)}</span>
              <span>目标 {fmt(trade.take_profit, 4)} · PnL {pct(trade.pnl)} · R {fmt(trade.r_multiple, 2)}</span>
            </div>
          ))}
          {!trades?.length && <StateBox type="empty" title="暂无纸上交易记录" text="需要 paper scan 和 mark 正常运行，才会继续积累前向样本。" />}
        </div>
      </Panel>
    </div>
  );
}

function IndicatorMapPage({ data, rows }: { data: LoadState; rows: CardRow[] }) {
  const catalogRows = indicatorMapRows(data, rows);
  const deepRows = catalogRows.filter((row) => row.stage === "已进评分" || row.stage === "候选/影子" || row.stage === "回测覆盖").length;
  const collectedRows = catalogRows.filter((row) => row.collected).length;
  return (
    <div className="pageStack">
      <section className="metricGrid compactMetrics">
        <Metric title="教程指标" value={fmt(catalogRows.length, 0)} detail="来自暗流教程规则表" tone="info" />
        <Metric title="已采集" value={fmt(collectedRows, 0)} detail="已有快照或特征样本" tone="good" />
        <Metric title="深度使用" value={fmt(deepRows, 0)} detail="进入评分、回测、候选或影子链路" tone="warn" />
        <Metric title="当前卡片" value={fmt(rows.length, 0)} detail="折叠后的交易计划" tone="info" />
      </section>
      <Panel title="指标教程映射" subtitle="看每个教程指标现在到了采集、评分、回测、候选哪一步">
        <div className="indicatorMapList">
          {catalogRows.map((item) => (
            <div className="indicatorRow" key={item.key}>
              <div>
                <strong>{item.name}</strong>
                <span>{item.key} · {familyText(item.family)}</span>
              </div>
              <StatusBadge tone={indicatorTone(item.stage)} label={item.stage} />
              <p>{item.summary}</p>
              <div className="indicatorStats">
                <small>采集 {fmt(item.snapshots, 0)}</small>
                <small>特征 {fmt(item.featureEvents, 0)}</small>
                <small>标注 {fmt(item.labels, 0)}</small>
                <small>候选 {fmt(item.candidateHits, 0)}</small>
              </div>
            </div>
          ))}
          {!catalogRows.length && <StateBox type="empty" title="教程映射未加载" text="请确认 /darkflow/rulebook 和 /signals/experiments 接口可用。" />}
        </div>
      </Panel>
      <Panel title="剧本关系" subtitle="教程指标不是孤立使用，而是按入场、确认、阻断、目标组合成策略剧本">
        <div className="playbookList">
          {(data.playbooks?.playbooks ?? []).map((item) => (
            <div className="playbookItem" key={item.key}>
              <strong>{item.display_name}</strong>
              <p>{item.thesis}</p>
              <div>
                <span>入场：{item.entry_indicators.map(indicatorName).join("、")}</span>
                <span>确认：{item.confirmation_indicators.map(indicatorName).join("、")}</span>
                <span>阻断：{item.blocker_indicators.map(indicatorName).join("、")}</span>
                <span>目标：{item.target_indicators.map(indicatorName).join("、")}</span>
              </div>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function DataFreshnessPage({ data, errors }: { data: LoadState; errors: LoadErrors }) {
  const freshness = data.entryStates?.freshness;
  const checks = [
    { label: "最新价格", age: freshness?.age_minutes?.price, time: freshness?.latest_price_at, ok: (freshness?.age_minutes?.price ?? 999) <= 60 },
    { label: "最新信号快照", age: freshness?.age_minutes?.signal_snapshot, time: freshness?.latest_signal_snapshot_at, ok: (freshness?.age_minutes?.signal_snapshot ?? 999) <= 60 },
    { label: "暗流管道", age: freshness?.age_minutes?.interaction_pipeline, time: freshness?.latest_pipeline_run_at, ok: (freshness?.age_minutes?.interaction_pipeline ?? 999) <= 30 },
    { label: "候选刷新", age: freshness?.age_minutes?.candidate_pipeline, time: freshness?.latest_candidate_updated_at, ok: (freshness?.age_minutes?.candidate_pipeline ?? 999) <= 30 },
    { label: "市场机会", age: freshness?.age_minutes?.interaction_event, time: freshness?.latest_interaction_event_at, ok: freshness?.opportunity_status !== "quiet" },
  ];
  return (
    <div className="pageStack">
      <Panel title="数据新鲜度" subtitle="判断是系统滞后，还是市场暂时没有新机会">
        <div className="freshnessList">
          {checks.map((item) => <FreshnessRow key={item.label} {...item} />)}
        </div>
      </Panel>
      {Object.keys(errors).length > 0 && <LoadErrorBanner errors={errors} />}
    </div>
  );
}

function SafetyPage({ safety }: { safety: TradingSafety | null }) {
  return (
    <div className="pageStack">
      <Panel title="安全开关" subtitle="本页只读展示，不提供开启实盘按钮">
        <SafetySummary safety={safety} />
      </Panel>
    </div>
  );
}

function PlaceholderPage({ title, text }: { title: string; text: string }) {
  return <Panel title={title} subtitle="暂未开放"><StateBox type="empty" title="模块待接入" text={text} /></Panel>;
}

function RadarScore({ row }: { row: CardRow }) {
  const ref = (node: HTMLDivElement | null) => {
    if (!node) return;
    const chart = echarts.getInstanceByDom(node) ?? echarts.init(node, undefined, { renderer: "canvas" });
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "item" },
      legend: { show: false },
      radar: {
        radius: "68%",
        splitNumber: 4,
        axisName: { color: "#9aa7b4", fontSize: 11 },
        splitLine: { lineStyle: { color: "#263241" } },
        splitArea: { areaStyle: { color: ["rgba(255,255,255,0.02)", "rgba(255,255,255,0.04)"] } },
        axisLine: { lineStyle: { color: "#263241" } },
        indicator: [
          { name: "质量", max: 100 },
          { name: "盈亏比", max: 100 },
          { name: "防重绘", max: 100 },
          { name: "趋势一致", max: 100 },
          { name: "暗流确认", max: 100 },
          { name: "影子样本", max: 100 },
        ],
      },
      series: [{
        type: "radar",
        symbolSize: 3,
        data: [
          { value: [80, 75, 85, 80, 75, 60], name: "理想通过信号", areaStyle: { color: "rgba(148,163,184,0.18)" }, lineStyle: { color: "rgba(148,163,184,0.55)" }, itemStyle: { color: "#94a3b8" } },
          { value: radarValues(row), name: "当前信号", areaStyle: { color: "rgba(22,199,132,0.22)" }, lineStyle: { color: "#16c784", width: 2 }, itemStyle: { color: "#16c784" } },
        ],
      }],
    });
  };
  return <div className="radarChart" ref={ref} />;
}

function EquityChart({ points }: { points: Array<{ time: string; equity: number }> }) {
  const ref = (node: HTMLDivElement | null) => {
    if (!node) return;
    const chart = echarts.getInstanceByDom(node) ?? echarts.init(node, undefined, { renderer: "canvas" });
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      grid: { left: 42, right: 16, top: 18, bottom: 28 },
      xAxis: { type: "category", data: points.map((item) => item.time), axisLabel: { color: "#8b98a7", hideOverlap: true }, axisLine: { lineStyle: { color: "#263241" } } },
      yAxis: { type: "value", axisLabel: { color: "#8b98a7" }, splitLine: { lineStyle: { color: "#1f2b38" } } },
      series: [{ type: "line", smooth: true, showSymbol: false, data: points.map((item) => item.equity), lineStyle: { color: "#16c784", width: 2 }, areaStyle: { color: "rgba(22,199,132,0.14)" } }],
    });
  };
  return <div className="equityChart" ref={ref} />;
}

type CardRow = {
  key: string;
  duplicateCount: number;
  duplicateKeys: string[];
  variantSummary: string;
  source: "card" | "candidate" | "entry";
  symbol: string;
  direction: string;
  interval: string;
  strategyId: string;
  setupType: string;
  state: string;
  stateRaw: string;
  reasonRaw: string;
  status: string;
  promotionStatus: string;
  auditStatus: string;
  shadowStatus: string;
  paperEligible: boolean;
  liveEligible: boolean;
  qualityScore: number;
  rrRatio: number;
  ruleScore: number;
  modelWinProb: number | null;
  expectedR: number | null;
  entryPrice: number;
  stopPrice: number;
  targetPrice: number;
  invalidationPrice?: number;
  markPrice?: number | null;
  entryRangeText: string;
  validUntil?: string | null;
  supportingSignals: string[];
  blockers: string[];
  promotionBlockers: string[];
  primaryReason: string;
  secondaryReason: string;
  decisionCard?: DecisionCard;
  candidate?: TradeCandidate;
  entrySample?: EntryPlanSample;
};

type ReasonItem = {
  title: string;
  text: string;
  group?: string;
  tone?: "normal" | "warn" | "good";
};

function buildRows(data: LoadState): CardRow[] {
  const byKey = new Map<string, CardRow>();
  for (const card of data.cards?.cards ?? []) {
    const key = card.card_id;
    byKey.set(key, rowFromCard(card));
  }
  for (const candidate of data.candidates?.candidates ?? []) {
    const key = candidate.candidate_key;
    const existing = byKey.get(key);
    byKey.set(key, mergeRows(existing, rowFromCandidate(candidate)));
  }
  for (const sample of data.entryStates?.samples ?? []) {
    const key = sample.candidate_key;
    const existing = byKey.get(key);
    byKey.set(key, mergeRows(existing, rowFromEntrySample(sample)));
  }
  return dedupeRows(Array.from(byKey.values())).sort((a, b) => scoreForSort(b) - scoreForSort(a));
}

function rowFromCard(card: DecisionCard): CardRow {
  const target = card.entry_plan.take_profit_levels[0];
  return {
    key: card.card_id,
    duplicateCount: 1,
    duplicateKeys: [card.card_id],
    variantSummary: "单一来源",
    source: "card",
    symbol: card.symbol,
    direction: card.direction,
    interval: card.interval,
    strategyId: card.strategy_id,
    setupType: card.setup_type,
    state: normalizeCardState(card.risk_gate.status),
    stateRaw: card.risk_gate.status,
    reasonRaw: card.risk_gate.blockers[0] ?? "waiting_for_entry_plan_state",
    status: card.risk_gate.status,
    promotionStatus: card.risk_gate.status,
    auditStatus: card.risk_gate.promotion_blockers.includes("anti_repaint_audit_missing") ? "missing" : "passed",
    shadowStatus: "not_started",
    paperEligible: card.risk_gate.paper_eligible,
    liveEligible: card.risk_gate.live_eligible,
    qualityScore: card.scores.quality_score,
    rrRatio: card.risk.rr_ratio,
    ruleScore: card.scores.rule_score,
    modelWinProb: card.scores.model_win_prob,
    expectedR: card.scores.expected_R,
    entryPrice: card.entry_plan.planned_entry,
    stopPrice: card.entry_plan.planned_stop,
    targetPrice: target?.price ?? card.entry_plan.planned_entry,
    invalidationPrice: card.entry_plan.invalidation_price,
    entryRangeText: entryRangeText(card.entry_plan),
    validUntil: card.entry_plan.valid_until,
    supportingSignals: card.supporting_signals,
    blockers: card.risk_gate.blockers,
    promotionBlockers: card.risk_gate.promotion_blockers,
    primaryReason: primaryReason(card.risk_gate.blockers, card.supporting_signals),
    secondaryReason: `固定计划至 ${timeText(card.entry_plan.valid_until)}`,
    decisionCard: card,
  };
}

function rowFromCandidate(candidate: TradeCandidate): CardRow {
  const card = candidate.decision_payload;
  return {
    key: candidate.candidate_key,
    duplicateCount: 1,
    duplicateKeys: [candidate.candidate_key],
    variantSummary: "单一来源",
    source: "candidate",
    symbol: candidate.symbol,
    direction: candidate.direction,
    interval: candidate.interval,
    strategyId: candidate.strategy_id,
    setupType: candidate.setup_type,
    state: normalizeCardState(candidate.status),
    stateRaw: candidate.status,
    reasonRaw: candidate.promotion_blockers[0] ?? candidate.blockers[0] ?? "candidate_materialized",
    status: candidate.status,
    promotionStatus: candidate.promotion_status,
    auditStatus: candidate.anti_repaint_status,
    shadowStatus: candidate.shadow_status,
    paperEligible: candidate.paper_eligible,
    liveEligible: candidate.live_eligible,
    qualityScore: candidate.quality_score,
    rrRatio: candidate.rr_ratio,
    ruleScore: card?.scores.rule_score ?? candidate.quality_score,
    modelWinProb: card?.scores.model_win_prob ?? null,
    expectedR: card?.scores.expected_R ?? null,
    entryPrice: candidate.entry_price,
    stopPrice: candidate.stop_price,
    targetPrice: candidate.target_price,
    invalidationPrice: card?.entry_plan.invalidation_price,
    entryRangeText: entryRangeText(card?.entry_plan) || "--",
    validUntil: card?.entry_plan.valid_until,
    supportingSignals: candidate.supporting_signals,
    blockers: candidate.blockers,
    promotionBlockers: candidate.promotion_blockers,
    primaryReason: primaryReason(candidate.blockers, candidate.supporting_signals),
    secondaryReason: `${promotionText(candidate.promotion_status)} · ${auditText(candidate.anti_repaint_status)}`,
    decisionCard: card,
    candidate,
  };
}

function rowFromEntrySample(sample: EntryPlanSample): CardRow {
  return {
    key: sample.candidate_key,
    duplicateCount: 1,
    duplicateKeys: [sample.candidate_key],
    variantSummary: "单一来源",
    source: "entry",
    symbol: sample.symbol,
    direction: sample.direction,
    interval: "30m",
    strategyId: "darkflow_entry_plan",
    setupType: "entry_plan_state",
    state: normalizeEntryState(sample.entry_plan_state.state),
    stateRaw: sample.entry_plan_state.state,
    reasonRaw: sample.entry_plan_state.reason,
    status: sample.status,
    promotionStatus: sample.promotion_status,
    auditStatus: sample.anti_repaint_status,
    shadowStatus: sample.shadow_status,
    paperEligible: false,
    liveEligible: false,
    qualityScore: sample.quality_score,
    rrRatio: riskReward(sample.entry_price, sample.stop_price, sample.target_price, sample.direction),
    ruleScore: sample.quality_score,
    modelWinProb: null,
    expectedR: null,
    entryPrice: sample.entry_price,
    stopPrice: sample.stop_price,
    targetPrice: sample.target_price,
    invalidationPrice: sample.entry_plan_state.invalidation_price,
    markPrice: sample.entry_plan_state.mark_price,
    entryRangeText: sampleRangeText(sample),
    validUntil: sample.entry_plan_state.valid_until,
    supportingSignals: [],
    blockers: [],
    promotionBlockers: [sample.entry_plan_state.reason],
    primaryReason: stateReasonText(sample.entry_plan_state.reason),
    secondaryReason: `当前价 ${fmt(sample.entry_plan_state.mark_price, 4)} · 有效至 ${timeText(sample.entry_plan_state.valid_until)}`,
    entrySample: sample,
  };
}

function mergeRows(base: CardRow | undefined, next: CardRow): CardRow {
  if (!base) return next;
  return {
    ...base,
    ...next,
    source: base.source === "card" ? base.source : next.source,
    decisionCard: base.decisionCard ?? next.decisionCard,
    candidate: base.candidate ?? next.candidate,
    entrySample: base.entrySample ?? next.entrySample,
    supportingSignals: unique([...base.supportingSignals, ...next.supportingSignals]),
    blockers: unique([...base.blockers, ...next.blockers]),
    promotionBlockers: unique([...base.promotionBlockers, ...next.promotionBlockers]),
    state: next.entrySample ? next.state : base.state,
    stateRaw: next.entrySample ? next.stateRaw : base.stateRaw,
    reasonRaw: next.entrySample ? next.reasonRaw : base.reasonRaw,
    markPrice: next.markPrice ?? base.markPrice,
    entryRangeText: next.entryRangeText !== "--" ? next.entryRangeText : base.entryRangeText,
  };
}

function dedupeRows(rows: CardRow[]): CardRow[] {
  const buckets = new Map<string, CardRow[]>();
  for (const row of rows) {
    const key = planDedupeKey(row);
    buckets.set(key, [...(buckets.get(key) ?? []), row]);
  }
  return Array.from(buckets.values()).map(mergeDuplicateRows);
}

function mergeDuplicateRows(items: CardRow[]): CardRow {
  const sorted = [...items].sort((a, b) => scoreForSort(b) - scoreForSort(a));
  const best = sorted[0];
  const duplicateKeys = unique(items.flatMap((item) => item.duplicateKeys.length ? item.duplicateKeys : [item.key]));
  const intervals = unique(items.map((item) => timeframeText(item.interval)));
  const states = unique(items.map((item) => entryStateText(item.state)));
  const validUntil = latestTime(items.map((item) => item.validUntil));
  return {
    ...best,
    duplicateCount: duplicateKeys.length,
    duplicateKeys,
    supportingSignals: unique(items.flatMap((item) => item.supportingSignals)),
    blockers: unique(items.flatMap((item) => item.blockers)),
    promotionBlockers: unique(items.flatMap((item) => item.promotionBlockers)),
    validUntil: validUntil ?? best.validUntil,
    primaryReason: primaryReason(unique(items.flatMap((item) => item.blockers)), unique(items.flatMap((item) => item.supportingSignals))),
    secondaryReason: duplicateKeys.length > 1
      ? `已合并 ${duplicateKeys.length} 条同计划 · ${states.slice(0, 2).join("/")} · ${intervals.join("/")}`
      : best.secondaryReason,
    variantSummary: duplicateKeys.length > 1
      ? `${duplicateKeys.length} 条暗流交互给出同一组入场/止损/目标，已折叠为一张卡；周期：${intervals.join("、") || "--"}；状态：${states.join("、") || "--"}`
      : "单一来源",
  };
}

function planDedupeKey(row: CardRow): string {
  return [
    row.symbol,
    row.direction,
    row.strategyId,
    roundKey(row.entryPrice),
    roundKey(row.stopPrice),
    roundKey(row.targetPrice),
    rangeKey(row.entryRangeText),
  ].join("|");
}

function filterRows(rows: CardRow[], filters: Filters): CardRow[] {
  return rows.filter((row) => {
    const query = filters.query.trim().toLowerCase();
    if (query && !`${row.symbol} ${strategyText(row.strategyId)} ${row.strategyId}`.toLowerCase().includes(query)) return false;
    if (filters.direction !== "all" && row.direction !== filters.direction) return false;
    if (filters.state !== "all") {
      if (filters.state === "blocked") {
        if (row.status !== "research_blocked" && row.promotionStatus !== "blocked") return false;
      } else if (filters.state === "shadow_candidate") {
        if (row.status !== "shadow_candidate") return false;
      } else if (row.state !== filters.state) {
        return false;
      }
    }
    if (row.qualityScore < filters.minQuality) return false;
    if (filters.onlyActionable && row.state !== "waiting" && row.state !== "triggered") return false;
    return true;
  });
}

function hasTradeEvidence(row: CardRow) {
  return Boolean(row.candidate || row.decisionCard || row.supportingSignals.length || row.blockers.length);
}

function relevantLoadErrors(page: PageId, errors: LoadErrors): LoadErrors {
  const shared: SectionKey[] = [];
  const byPage: Record<PageId, SectionKey[]> = {
    overview: ["summary", "quality", "candidates", "entryStates", "darkflow", "safety"],
    tradeCards: ["cards", "candidates", "entryStates"],
    candidates: ["candidates"],
    entryPlans: ["entryStates"],
    experimentLab: ["indicatorCoverage", "experimentEffectiveness", "featurePaperAb", "featureSegmentPaperAb"],
    backtest: ["darkflow", "backtestsLatest", "playbookBacktest"],
    paperTrading: ["paperStats", "paperTrades"],
    shadow: ["shadow", "shadowTrades"],
    indicatorMap: ["rulebook", "playbooks", "indicatorCoverage"],
    dataFreshness: ["entryStates"],
    safety: ["safety"],
    logs: [],
  };
  const allowed = new Set<SectionKey>([...shared, ...(byPage[page] ?? [])]);
  return Object.fromEntries(Object.entries(errors).filter(([key]) => allowed.has(key as SectionKey))) as LoadErrors;
}

function Pagination({ page, pageSize, total, totalPages, onPage, onPageSize }: { page: number; pageSize: number; total: number; totalPages: number; onPage: (value: number) => void; onPageSize: (value: number) => void }) {
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(total, page * pageSize);
  return (
    <div className="pagination">
      <span>第 {start}-{end} 条，共 {total} 条</span>
      <div className="pageControls">
        <button onClick={() => onPage(Math.max(1, page - 1))} disabled={page <= 1}><ChevronLeft size={16} />上一页</button>
        <strong>{page} / {totalPages}</strong>
        <button onClick={() => onPage(Math.min(totalPages, page + 1))} disabled={page >= totalPages}>下一页<ChevronRight size={16} /></button>
      </div>
      <label>
        每页
        <select value={pageSize} onChange={(event) => onPageSize(Number(event.target.value))}>
          {PAGE_SIZE_OPTIONS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
    </div>
  );
}

function LoadErrorBanner({ errors }: { errors: LoadErrors }) {
  return (
    <div className="alert">
      <AlertTriangle size={18} />
      <div>
        <strong>部分模块加载失败</strong>
        <span>{Object.entries(errors).map(([key, value]) => `${sectionLabel(key as SectionKey)}：${value}`).join("；")}</span>
      </div>
    </div>
  );
}

function Metric({ title, value, detail, tone = "info" }: { title: string; value: React.ReactNode; detail: string; tone?: string }) {
  return <div className={`metric ${tone}`}><span>{title}</span><strong>{value ?? "--"}</strong><small>{detail}</small></div>;
}

function Panel({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return <section className="panel"><div className="panelHeader"><div><h2>{title}</h2><span>{subtitle}</span></div></div>{children}</section>;
}

function Level({ label, value, danger = false }: { label: string; value?: number | null; danger?: boolean }) {
  return <div className="level"><span>{label}</span><strong className={danger ? "danger" : ""}>{fmt(value, 4)}</strong></div>;
}

function ScoreCell({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function LevelLine({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return <div className={danger ? "dangerText" : ""}><span>{label}</span><strong>{value}</strong></div>;
}

function StatusBadge({ label, tone }: { label: string; tone: string }) {
  return <span className={`statusBadge ${tone}`}>{label}</span>;
}

function StateBox({ type, title, text, action, onAction }: { type: "empty" | "loading" | "error"; title: string; text: string; action?: string; onAction?: () => void }) {
  const Icon = type === "error" ? XCircle : type === "loading" ? RefreshCcw : SlidersHorizontal;
  return <div className={`stateBox ${type}`}><Icon size={20} /><div><strong>{title}</strong><span>{text}</span>{action && <button onClick={onAction}>{action}</button>}</div></div>;
}

function ResearchArm({ title, stats }: { title: string; stats?: ResearchArmStats | null }) {
  return (
    <div className="researchArm">
      <strong>{title}</strong>
      <div>
        <span>样本</span><em>{fmt(stats?.trade_count, 0)}</em>
      </div>
      <div>
        <span>胜率</span><em>{pct(stats?.win_rate)}</em>
      </div>
      <div>
        <span>盈利因子</span><em>{fmt(stats?.profit_factor, 2)}</em>
      </div>
      <div>
        <span>平均收益</span><em>{pct(stats?.avg_return)}</em>
      </div>
    </div>
  );
}

function ActionItem({ icon: Icon, title, text, action, onClick }: { icon: typeof Gauge; title: string; text: string; action: string; onClick: () => void }) {
  return <div className="actionItem"><Icon size={18} /><div><strong>{title}</strong><span>{text}</span></div><button onClick={onClick}>{action}</button></div>;
}

function AuditItem({ label, ok, text }: { label: string; ok: boolean; text: string }) {
  return <div className={`auditItem ${ok ? "ok" : "block"}`}>{ok ? <CheckCircle2 size={16} /> : <XCircle size={16} />}<span>{label}</span><strong>{text}</strong></div>;
}

function ReasonList({ title, items, empty, warn = false }: { title: string; items: ReasonItem[]; empty: string; warn?: boolean }) {
  const visible: ReasonItem[] = items.length ? items : [{ title: empty, text: "当前还没有可解释的细分证据。", tone: warn ? "warn" : "normal" }];
  return (
    <div className="reasonList">
      <strong>{title}</strong>
      {visible.map((item, index) => (
        <div className={`reasonItem ${warn || item.tone === "warn" ? "warn" : item.tone === "good" ? "good" : ""}`} key={`${item.title}-${index}`}>
          {item.group && <em>{item.group}</em>}
          <span>{item.title}</span>
          <p>{item.text}</p>
        </div>
      ))}
    </div>
  );
}

function CodeItem({ label, value }: { label: string; value?: string }) {
  return <div><span>{label}</span><code>{value || "--"}</code></div>;
}

function FreshnessRow({ label, age, time, ok }: { label: string; age?: number | null; time?: string | null; ok: boolean }) {
  return <div className="freshnessRow"><span className={`statusDot ${ok ? "fresh" : "stale pulse"}`} /><strong>{label}</strong><span>{ageText(age)}</span><small>{timeText(time)}</small></div>;
}

function Definition({ title, code, text }: { title: string; code: string; text: string }) {
  return <div className="definition"><strong>{title}</strong><code>{code}</code><span>{text}</span></div>;
}

function SafetySummary({ safety }: { safety: TradingSafety | null }) {
  return <div className="safetyGrid"><AuditItem label="实盘交易" ok={Boolean(safety?.live_trading_enabled)} text={safety?.live_trading_enabled ? "开启" : "关闭"} /><AuditItem label="熔断开关" ok={Boolean(safety?.kill_switch_active)} text={safety?.kill_switch_active ? "已开启" : "未开启"} /><AuditItem label="人工确认" ok={Boolean(safety?.manual_confirmation_required)} text={safety?.manual_confirmation_required ? "必须确认" : "未要求"} /></div>;
}

function SegmentedControl({ label, value, options, onChange }: { label: string; value: string; options: Array<{ label: string; value: string }>; onChange: (value: string) => void }) {
  return <div className="segmented"><span>{label}</span><div>{options.map((option) => <button key={option.value} className={value === option.value ? "active" : ""} onClick={() => onChange(option.value)}>{option.label}</button>)}</div></div>;
}

function errorText(err: unknown): string { return err instanceof Error ? err.message : String(err); }
function fmt(value: unknown, digits = 2): string { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--"; }
function pct(value: unknown): string { return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "--"; }
function qualityText(value?: string) { return value === "ok" ? "正常" : value === "warning" ? "警告" : value === "error" ? "异常" : "未知"; }
function directionText(value: string) { return value === "long" ? "做多" : value === "short" ? "做空" : value; }
function timeframeText(value: string) { return value === "30m" ? "30分钟" : value === "1h" ? "1小时" : value === "4h" ? "4小时" : value; }
function strategyText(value: string) { return ({ pullback_to_cost: "成本带回踩", liquidity_sweep_reversal: "扫损反转", breakout_confirmation: "突破确认", trend_ride_extension: "趋势延展", darkflow_entry_plan: "冻结入场计划" } as Record<string, string>)[value] ?? value.replace(/_/g, " "); }
function signalText(value: string) { return signalDetail(value).title; }
function signalDetail(value: string): ReasonItem {
  return ({
    official_rule_mapped: { group: "形态依据", title: "符合教程定义的暗流形态", text: "该候选不是随便给出的价格点，而是被映射到教程里的成本带、清算带、筹码区或趋势结构规则。", tone: "good" },
    dynamic_darkflow_target: { group: "目标依据", title: "目标价来自相邻暗流区域", text: "止盈目标来自附近清算磁吸区、筹码区或教程目标区，不是固定猜一个百分比。", tone: "good" },
    first_touch_zone_reaction: { group: "位置依据", title: "第一次触及关键区域并出现反应", text: "价格刚触碰教程定义的关键区域，系统把它当作可观察反应点；多次触碰后的可靠性会降低。", tone: "good" },
    parent_trend_aligned: { group: "趋势依据", title: "方向与更高周期趋势一致", text: "当前做多/做空方向没有和更大周期趋势冲突，因此趋势过滤没有扣分。", tone: "good" },
    trend_extension_available: { group: "持仓依据", title: "趋势仍有延展空间", text: "趋势强度或暗流燃料还没有显示明显耗尽，适合继续观察延展，而不是急着提前止盈。", tone: "good" },
    wick_reclaim_after_sweep: { group: "扫损依据", title: "扫损后价格快速收回", text: "价格刺穿清算/止损区域后没有继续同向，而是快速收回，按教程更接近假突破或洗盘后的反向机会。", tone: "good" },
    tutorial_allows_single_trigger: { group: "触发规则", title: "该类形态允许单次触发进入观察", text: "教程规则里这类信号可以先用一次触发建立观察，但仍要等待风控、影子样本和有效入场区间确认。", tone: "normal" },
    confirmation_indicators_nearby: { group: "确认依据", title: "附近有确认类指标共振", text: "入场区域附近出现订单流、清算或结构类确认信号，说明不是孤立价格点。", tone: "good" },
  } as Record<string, ReasonItem>)[value] ?? { title: readableCode(value), text: "系统返回了新的支持信号码，当前先按原始含义展示，后续可补充教程解释。" };
}
function blockerText(value: string) { return blockerDetail(value).title; }
function blockerDetail(value: string): ReasonItem {
  return ({
    anti_repaint_audit_missing: { group: "审计", title: "缺少防重绘审计", text: "还没有确认这个信号在后续快照中稳定存在，不能把可能重绘的信号用于开仓。", tone: "warn" },
    isolated_v2_shadow_forward_sample_missing: { group: "样本", title: "缺少隔离影子样本", text: "还没有在独立影子交易里积累足够的前向样本，暂时只能研究观察。", tone: "warn" },
    isolated_v2_shadow_forward_sample_collecting: { group: "样本", title: "影子样本采集中", text: "该候选已经进入影子观察，但样本数量或结果还不足以晋级。", tone: "warn" },
    isolated_v2_shadow_forward_sample_weak: { group: "样本", title: "影子样本表现不足", text: "前向影子表现没有达到胜率、盈利因子或回撤门槛，暂不晋级。", tone: "warn" },
    isolated_v2_shadow_forward_sample_failed: { group: "样本", title: "影子样本未达标", text: "前向影子样本已经闭合，但胜率、盈利因子或回撤没有达到晋级门槛。", tone: "warn" },
    entry_plan_retired: { group: "入场计划", title: "入场计划已退休", text: "原来的冻结入场区间已经过期、错过或失效，不能继续追价，需要等待新的暗流信号。", tone: "warn" },
    duplicate_shadow_forward_plan: { group: "样本", title: "重复影子计划", text: "同一币种、方向、策略和相近入场计划已经在影子采样中，系统不再重复开样本，避免统计被重复信号污染。", tone: "warn" },
    parent_trend_conflict: { group: "趋势", title: "与更高周期趋势冲突", text: "入场方向和更大周期趋势相反，容易变成逆势接刀或逆势摸顶。", tone: "warn" },
    quality_score_below_threshold: { group: "评分", title: "质量评分不足", text: "当前综合评分低于候选晋级门槛，可能是确认不足、趋势冲突或样本证据不够。", tone: "warn" },
    rr_ratio_below_threshold: { group: "风控", title: "盈亏比不足", text: "计划止损和目标之间的收益空间不够，即使方向对也不值得承担这笔风险。", tone: "warn" },
    fixed_r_target_fallback: { group: "目标", title: "目标价退回固定 R", text: "附近没有找到合格教程目标区，只能用固定 R 估算，因此目标可信度较低。", tone: "warn" },
    body_break_invalidation: { group: "失效", title: "实体破坏结构", text: "K 线实体已经穿过关键结构，原来的支撑/压制假设失效。", tone: "warn" },
    blocker_indicators_nearby: { group: "阻断", title: "附近有阻断类指标", text: "入场区域附近出现耗尽、反向大单或结构破坏信号，需要暂停追踪。", tone: "warn" },
  } as Record<string, ReasonItem>)[value] ?? { title: readableCode(value), text: "系统返回了新的阻断码，当前先按原始含义展示，后续可补充风控解释。", tone: "warn" };
}
function promotionText(value: string) { return ({ blocked: "研究阻断", shadow_ready_pending_audit: "待防重绘审计", shadow_forward_pending: "待影子入场", shadow_forward_collecting: "影子样本采集中", shadow_forward_failed: "影子样本未达标", entry_plan_retired: "入场计划已退休", duplicate_shadow_plan: "重复影子计划", shadow_running: "影子运行中", paper_review_ready: "待人工复核" } as Record<string, string>)[value] ?? value.replace(/_/g, " "); }
function auditText(value: string) { return ({ missing: "缺失", passed: "通过", failed: "失败" } as Record<string, string>)[value] ?? value; }
function shadowText(value: string) { return ({ not_started: "未开始", collecting: "采集中", retired: "已退休", failed: "未达标", passed: "已达标", closed: "已结束" } as Record<string, string>)[value] ?? value; }
function entryStateText(value: string) { return ({ triggered: "已触发", waiting: "等待入场", missed: "已错过", expired: "时间过期", invalidated: "条件作废", missing_price: "缺少价格", invalid_shape: "形态异常", entry_plan_retired: "入场计划已退休", blocked: "研究阻断", shadow_candidate: "影子候选" } as Record<string, string>)[value] ?? value.replace(/_/g, " "); }
function stateReasonText(value: string) { return ({ mark_price_inside_frozen_entry_range: "价格进入冻结入场区间", awaiting_frozen_entry_range: "尚未进入冻结入场区间", entry_range_missed: "价格已越过入场区间", valid_until_passed: "超过有效期", price_crosses_invalidation: "触发失效价", missing_latest_price: "缺少最新价格", invalid_long_frozen_entry_range: "多头入场区间异常", invalid_short_frozen_entry_range: "空头入场区间异常" } as Record<string, string>)[value] ?? blockerText(value); }
function sectionLabel(key: SectionKey) { return ({ summary: "系统摘要", quality: "数据质量", cards: "交易卡片", candidates: "候选池", entryStates: "入场计划", darkflow: "暗流交互回测", backtestsLatest: "批量回测", playbookBacktest: "剧本回测", paperStats: "纸上统计", paperTrades: "纸上交易明细", shadow: "影子纸上", shadowTrades: "影子交易明细", rulebook: "教程规则", playbooks: "策略剧本", indicatorCoverage: "指标覆盖", experimentEffectiveness: "实验有效性", featurePaperAb: "特征纸上 A/B", featureSegmentPaperAb: "分段纸上 A/B", safety: "安全开关" } as Record<SectionKey, string>)[key]; }
function pageLabel(page: PageId) { return NAV_GROUPS.flatMap((group) => group.items).find((item) => item.id === page)?.label ?? page; }
function pageTitleFromHash() { return pageLabel(pageFromHash()); }
function pageFromHash(): PageId { const raw = window.location.hash.replace("#", ""); return NAV_GROUPS.flatMap((group) => group.items).some((item) => item.id === raw) ? raw as PageId : "overview"; }
function timeText(value?: string | null) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "等待数据"; }
function ageText(value?: number | null) { if (typeof value !== "number" || !Number.isFinite(value)) return "--"; if (value < 1) return "小于1分钟"; if (value < 120) return `${Math.round(value)}分钟`; return `${(value / 60).toFixed(1)}小时`; }
function entryRangeText(plan?: FrozenEntryPlan | null) { const lower = plan?.entry_range?.lower; const upper = plan?.entry_range?.upper; return typeof lower === "number" && typeof upper === "number" ? `${fmt(lower, 4)} ~ ${fmt(upper, 4)}` : "--"; }
function sampleRangeText(sample: EntryPlanSample) { const lower = sample.entry_plan_state.entry_range?.lower; const upper = sample.entry_plan_state.entry_range?.upper; return typeof lower === "number" && typeof upper === "number" ? `${fmt(lower, 4)} ~ ${fmt(upper, 4)}` : "--"; }
function stateTone(value: string) { if (value === "triggered" || value === "shadow_candidate") return "good"; if (value === "waiting") return "info"; if (value === "missed" || value === "expired") return "warn"; return "bad"; }
function stateHint(value: string) { return ({ waiting: "仍可观察", missed: "不追价", expired: "时间失效", invalidated: "条件破坏", triggered: "进入区间", missing_price: "补价格", invalid_shape: "需修正" } as Record<string, string>)[value] ?? "--"; }
function normalizeEntryState(value: string) { return value === "invalid" ? "invalidated" : value; }
function normalizeCardState(value: string) { return value === "shadow_candidate" ? "shadow_candidate" : value === "research_blocked" ? "blocked" : value; }
function primaryReason(blockers: string[], signals: string[]) { if (signals.length) return signalSummary(signals); if (blockers.length) return blockerText(blockers[0]); return "等待更多暗流确认"; }
function signalSummary(signals: string[]) { return unique(signals.map(signalText)).slice(0, 2).join(" + "); }
function riskReward(entry: number, stop: number, target: number, direction: string) { const risk = Math.abs(entry - stop); const reward = direction === "short" ? entry - target : target - entry; return risk > 0 ? Math.max(0, reward / risk) : 0; }
function radarValues(row: CardRow) { return [clamp(row.qualityScore), clamp(row.rrRatio * 28), row.auditStatus === "passed" ? 90 : 25, row.supportingSignals.includes("parent_trend_aligned") ? 85 : 45, clamp(row.supportingSignals.length * 18), row.shadowStatus === "collecting" ? 80 : 25]; }
function tradeConclusion(row: CardRow) { if (row.state === "waiting") return "价格尚未进入冻结入场区间，可以继续观察，但不能提前追价。"; if (row.state === "missed") return "价格已经越过原计划入场区间，当前计划不建议追单。"; if (row.state === "expired") return "该计划已经超过有效期，需要等待新一轮暗流信号。"; if (row.state === "invalidated") return "价格或结构已经触发失效条件，该计划作废。"; if (row.status === "research_blocked" || row.state === "blocked") return "该候选仍处于研究阻断状态，主要原因见风控审计。"; return "该候选仅进入研究或影子观察边界，实盘仍关闭。"; }
function scoreForSort(row: CardRow) { const stateBoost = ({ triggered: 320, waiting: 300, shadow_candidate: 240, missed: 120, expired: 80, invalidated: 40, blocked: 20 } as Record<string, number>)[row.state] ?? 0; const shadowBoost = row.shadowStatus === "collecting" ? 35 : 0; return stateBoost + shadowBoost + row.qualityScore + row.rrRatio * 10; }
function shortKey(value: string) { return value.length > 18 ? `${value.slice(0, 10)}...${value.slice(-6)}` : value; }
function unique<T>(items: T[]) { return Array.from(new Set(items.filter(Boolean))); }
function clamp(value: number) { return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0)); }
function countBy<T>(items: T[], key: (item: T) => string) { return items.reduce<Record<string, number>>((acc, item) => { const id = key(item); acc[id] = (acc[id] ?? 0) + 1; return acc; }, {}); }

function roundKey(value: number) { return Number.isFinite(value) ? value.toPrecision(8) : "--"; }
function rangeKey(value: string) { return value.replace(/\s+/g, ""); }
function readableCode(value: string) { return value ? value.replace(/_/g, " ") : "未知状态"; }
function latestTime(values: Array<string | null | undefined>) {
  const times = values.filter(Boolean).map((value) => ({ value: value as string, ms: new Date(value as string).getTime() })).filter((item) => Number.isFinite(item.ms));
  return times.sort((a, b) => b.ms - a.ms)[0]?.value;
}

function candidateGroups(rows: CardRow[]) {
  const buckets = new Map<string, CardRow[]>();
  for (const row of rows) {
    const key = [row.symbol, row.direction, row.strategyId].join("|");
    buckets.set(key, [...(buckets.get(key) ?? []), row]);
  }
  return Array.from(buckets.entries()).map(([key, groupRows]) => {
    const sorted = [...groupRows].sort((a, b) => scoreForSort(b) - scoreForSort(a));
    const best = sorted[0];
    const blockers = topCounts(groupRows.flatMap((row) => [...row.blockers, ...row.promotionBlockers]).map(blockerText), 3);
    return {
      key,
      rows: groupRows,
      best,
      symbol: best.symbol,
      direction: best.direction,
      strategyId: best.strategyId,
      maxQuality: Math.max(...groupRows.map((row) => row.qualityScore)),
      maxRr: Math.max(...groupRows.map((row) => row.rrRatio)),
      latestState: entryStateText(best.state),
      reason: blockers.length ? `主要阻断：${blockers.join("、")}` : `主要依据：${unique(groupRows.flatMap((row) => row.supportingSignals).map(signalText)).slice(0, 3).join("、") || "等待更多确认"}`,
    };
  }).sort((a, b) => scoreForSort(b.best) - scoreForSort(a.best));
}

function topCounts(items: string[], limit: number) {
  const counts = countBy(items, (item) => item);
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, limit).map(([item]) => item);
}

function shadowEquitySeries(trades: ShadowTrade[]) {
  const closed = trades
    .filter((item) => item.status === "closed" && typeof item.pnl === "number")
    .sort((a, b) => new Date(a.closed_at || a.opened_at || 0).getTime() - new Date(b.closed_at || b.opened_at || 0).getTime());
  let equity = 1;
  const points = [{ time: "起点", equity }];
  for (const trade of closed) {
    equity *= 1 + Number(trade.pnl || 0) / 100;
    points.push({ time: timeShort(trade.closed_at || trade.opened_at), equity: Number(equity.toFixed(4)) });
  }
  return points;
}

function paperEquitySeries(trades: PaperTrade[]) {
  const closed = trades
    .filter((item) => item.status === "closed" && typeof item.pnl === "number")
    .sort((a, b) => new Date(a.closed_at || a.opened_at || 0).getTime() - new Date(b.closed_at || b.opened_at || 0).getTime());
  let equity = 1;
  const points = [{ time: "起点", equity }];
  for (const trade of closed) {
    equity *= 1 + Number(trade.pnl || 0);
    points.push({ time: timeShort(trade.closed_at || trade.opened_at), equity: Number(equity.toFixed(5)) });
  }
  return points;
}

function segmentAbConclusion(report?: FeaturePaperAbReport | null) {
  if (!report) return "分段 A/B 报告尚未加载，暂时不能判断候选组是否优于对照组。";
  const selected = Number(report.selected_candidate_count ?? 0);
  if (!selected) return "当前没有通过质量门槛的分段候选，应该继续增加跨天、跨 run、不同市场窗口样本，而不是降低门槛。";
  const candidate = report.arms?.candidate;
  const control = report.arms?.matched_control ?? report.arms?.all_control ?? report.arms?.control;
  const risk = report.quality?.candidate?.overfit_risk;
  const better = Number(candidate?.profit_factor ?? 0) > Number(control?.profit_factor ?? 0) && Number(candidate?.win_rate ?? 0) > Number(control?.win_rate ?? 0);
  if (better && risk !== "high") return "候选组暂时优于对照组，可以继续进入影子前向样本，但仍不能直接用于实盘。";
  if (better) return "候选组表现优于对照，但样本风险仍高；下一步应提高新候选产生速度和影子样本覆盖。";
  return "候选组相对对照优势不明显，暂时不能把这些指标提升为开仓权重。";
}

function researchStatusText(value?: string) {
  return ({ ready: "已有可读报告", no_candidate_features: "无达标候选", no_segment_candidate_features: "无达标分段", no_materialized_report: "未生成报告" } as Record<string, string>)[value || ""] ?? readableCode(value || "等待数据");
}

function experimentStatusText(value?: string) {
  return ({ candidate: "候选", noise_candidate: "噪声候选", ready: "可观察", collecting: "采集中" } as Record<string, string>)[value || ""] ?? readableCode(value || "研究中");
}

function experimentTone(value?: string) {
  if (value === "candidate" || value === "ready") return "good";
  if (value === "noise_candidate") return "warn";
  return "info";
}

function experimentRecommendationText(item: ExperimentIndicator) {
  if (item.used_for_opening_decisions) return "已经进入开仓决策链路，但仍要继续看前向纸上表现。";
  if (item.status === "candidate") return "研究表现不错，但仍是候选特征，需要通过纸上 A/B 和影子前向验证后才能晋级。";
  if (item.status === "noise_candidate") return "当前更像噪声，需要换转换方式或增加行情分段过滤。";
  return item.recommendation || "仍处于研究观察阶段，不能直接用于实盘或纸上开仓。";
}

function playbookText(value: string) {
  return ({ pullback_to_cost: "成本带回踩", liquidity_sweep_reversal: "扫损反转", breakout_confirmation: "突破确认", trend_ride_extension: "趋势延展", darkflow_entry_plan: "冻结入场计划" } as Record<string, string>)[value] ?? strategyText(value);
}

function tradeStatusText(value: string) {
  return ({ open: "持仓中", closed: "已平仓", stopped: "止损", take_profit: "止盈" } as Record<string, string>)[value] ?? readableCode(value);
}

function timeShort(value?: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "--";
}

function indicatorMapRows(data: LoadState, rows: CardRow[]) {
  const coverage = new Map((data.indicatorCoverage?.indicator_catalog ?? []).map((item) => [item.key, item]));
  const candidateHits = countBy(rows.flatMap((row) => inferCandidateIndicators(row)), (item) => item);
  const rules = data.rulebook?.rules ?? [];
  return rules.map((rule) => {
    const keys = unique([rule.official_key, ...rule.internal_keys]);
    const cover = keys.map((key) => coverage.get(key)).find(Boolean);
    const hits = keys.reduce((sum, key) => sum + (candidateHits[key] ?? 0), 0);
    const snapshots = Number(cover?.snapshot_count ?? 0);
    const featureEvents = Number(cover?.feature_event_count ?? 0);
    const labels = Number(cover?.feature_labeled_count ?? 0);
    const usedForScoring = Boolean(cover?.required_for_scoring || cover?.used_for_opening_decisions);
    const usedInBacktest = Boolean(cover?.used_in_backtest || labels > 0);
    const stage = hits > 0 ? "候选/影子" : usedForScoring ? "已进评分" : usedInBacktest ? "回测覆盖" : snapshots > 0 || featureEvents > 0 ? "已采集" : "待接入";
    return {
      key: rule.official_key,
      name: rule.official_name || indicatorName(rule.official_key),
      family: rule.family,
      stage,
      collected: snapshots > 0 || featureEvents > 0,
      snapshots,
      featureEvents,
      labels,
      candidateHits: hits,
      summary: rule.primary_roles?.includes("target")
        ? `主要用于目标/止盈：${rule.target_rule}`
        : `主要用于入场或过滤：${rule.long_rule}`,
    };
  });
}

function inferCandidateIndicators(row: CardRow) {
  const strategyMap: Record<string, string[]> = {
    pullback_to_cost: ["smart_money_cost", "trend_price", "micro_poc", "hvn_nodes", "inst_vwap"],
    liquidity_sweep_reversal: ["liquidity_sweep", "liq_heatmap", "retail_stop_loss", "cascade_liquidation_zones"],
    breakout_confirmation: ["inst_choch", "cross_exchange_resonance", "imbalance"],
    trend_ride_extension: ["inst_vwap", "trailing_vwap", "smart_money_cost", "trend_price", "liquidation_fuel"],
    darkflow_entry_plan: [],
  };
  return strategyMap[row.strategyId] ?? [];
}

function indicatorName(value: string) {
  return ({
    smart_money_cost: "趋势成本带",
    liq_heatmap: "清算热力图",
    liquidation_fuel: "密集博弈",
    liquidity_sweep: "极限洗盘深度",
    cross_exchange_resonance: "主力大单行动",
    imbalance: "订单簿衰减",
    trend_exhaustion: "趋势时间极限",
    trend_price: "趋势撑压",
    inst_vwap: "趋势动态防线",
    inst_volume_profile: "筹码分布",
    hvn_nodes: "高成交节点",
    micro_poc: "微观成本线",
    fair_value_gap: "筹码真空区",
    retail_stop_loss: "散户止损点",
    cascade_liquidation_zones: "连环爆仓区",
    trailing_vwap: "趋势动态防线",
    trend_roi: "未来收益预期",
    liquidity_vacuum: "流动性黑洞预警",
    inst_choch: "破坏与突破",
    ob_decay: "订单墙衰减",
    trend_purity: "趋势筹码纯度",
    poc_shift: "均价重心偏移",
    max_pain: "极限洗盘深度",
    time_exhaustion: "趋势时间极限",
    volume_exhaustion: "趋势资金阈值",
    max_drawdown_tolerance: "涨跌极限",
  } as Record<string, string>)[value] ?? readableCode(value);
}

function familyText(value?: string) {
  return ({ cost_structure: "成本结构", liquidity: "流动性", orderflow: "订单流", lifecycle: "趋势生命周期", structure_break: "结构突破", volume_profile: "筹码分布", institutional_flow: "机构均价", vacuum: "真空/加速", exhaustion: "趋势耗尽", orderflow_structure: "订单流结构" } as Record<string, string>)[value || ""] ?? readableCode(value || "未知分类");
}

function indicatorTone(stage: string) {
  if (stage === "候选/影子" || stage === "已进评分") return "good";
  if (stage === "回测覆盖" || stage === "已采集") return "info";
  return "warn";
}

createRoot(document.getElementById("root")!).render(<App />);
