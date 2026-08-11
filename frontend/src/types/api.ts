export interface PortfolioSummary {
  bankroll: number;
  peak_bankroll: number;
  open_position_count: number;
  total_exposure: number;
  unrealized_pnl: number;
  total_return_pct: number;
  max_drawdown_pct: number;
}

export interface Position {
  market_id: string;
  title: string;
  side: string;
  entry_price: number;
  current_price: number;
  quantity: number;
  unrealized_pnl: number;
  cost_basis: number;
  opened_at: string | null;
}

export interface Trade {
  market_id: string;
  title: string;
  side: string;
  action: string;
  price: number;
  quantity: number;
  p_model: number;
  implied_prob: number;
  edge: number;
  net_ev: number;
  position_size_dollars: number;
  confidence: number;
  reasoning: string;
  is_paper: boolean;
  status: string;
  exit_price: number | null;
  realized_pnl: number | null;
  created_at: string;
}

export interface Metrics {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_pnl: number;
  total_return_pct: number;
  avg_edge: number;
  avg_ev: number;
  calibration_error: number;
  avg_pnl_per_trade: number;
}

export interface EquityPoint {
  timestamp: string | null;
  bankroll: number;
  peak: number;
  market_id?: string;
  pnl?: number;
}

export interface Market {
  market_id: string;
  title: string;
  category: string;
  sub_category: string | null;
  close_date: string;
  status: string;
  rules: string | null;
}

export interface Opportunity {
  market_id: string;
  p_model: number;
  implied_prob: number;
  edge: number;
  net_ev: number;
  recommended_side: string;
  confidence: number;
  status: string;
  reasoning: string | null;
  model_name: string;
}

export interface BacktestRun {
  id: number;
  start_date: string;
  end_date: string;
  initial_bankroll: number;
  final_bankroll: number;
  total_trades: number;
  total_pnl: number;
  status: string;
  created_at: string;
}

export interface BacktestRunResult {
  run_id: number;
  status: string;
  total_trades: number;
  total_pnl: number;
  final_bankroll: number;
}

export interface BacktestReport {
  run_id: number;
  start_date: string;
  end_date: string;
  initial_bankroll: number;
  final_bankroll: number;
  total_trades: number;
  wins: number;
  losses: number;
  total_pnl: number;
  total_return_pct: number;
  win_rate: number;
  max_drawdown_pct: number;
  avg_ev: number;
  avg_edge: number;
  calibration_error: number;
  equity_curve: EquityPoint[];
  limitations: string[];
}

export interface SystemStatus {
  mode: string;
  version: string;
}

export interface MarketMatch {
  id: number;
  kalshi_market_id: string;
  poly_condition_id: string;
  status: string;
  similarity: number;
  kalshi_title: string | null;
  poly_question: string | null;
  verdict: string | null;
  reason: string | null;
  decided_by: string | null;
  created_at: string | null;
}

export interface OddsCacheEntry {
  sport_key: string;
  source: string;
  fetched_at: string;
  age_minutes: number;
}

export interface QuotaStatus {
  month: string;
  source: string;
  used: number;
  cap: number;
  remaining: number;
  burn_per_day: number;
  projected_month_end: number;
  days_in_month: number;
  days_elapsed: number;
  days_to_exhaustion: number | null;
  projected_overrun: boolean;
  fallback_enabled: boolean;
  cache: OddsCacheEntry[];
}

export interface PriceSnapshot {
  yes_bid: number;
  yes_ask: number;
  last_price: number;
  volume: number;
  timestamp: string;
}
