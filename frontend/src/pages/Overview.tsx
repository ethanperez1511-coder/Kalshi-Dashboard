import { useApi } from '../hooks/useApi';
import KpiCard from '../components/KpiCard';
import Panel from '../components/Panel';
import type { PortfolioSummary, Position, Trade, Metrics, EquityPoint } from '../types/api';
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ScatterChart, Scatter, ReferenceLine, CartesianGrid,
} from 'recharts';

function fmt(n: number, dec = 2) { return n.toFixed(dec); }
function fmtUsd(n: number) { return `$${n.toFixed(2)}`; }
function fmtPct(n: number) { return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`; }

function pnlColor(n: number) {
  if (n > 0) return 'var(--positive)';
  if (n < 0) return 'var(--negative)';
  return 'var(--faint)';
}

/* Line icons for KPI cards */
const IconWallet = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="6" width="20" height="14" rx="2" /><path d="M2 10h20" /><path d="M16 14h2" />
  </svg>
);
const IconTrend = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" /><polyline points="16 7 22 7 22 13" />
  </svg>
);
const IconLayers = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" />
  </svg>
);
const IconShield = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
  </svg>
);
const IconActivity = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
  </svg>
);

const tooltipStyle = {
  background: 'var(--panel2)',
  border: '1px solid var(--hairline)',
  borderRadius: 8,
  fontFamily: 'var(--font-mono)',
  fontSize: '0.72rem',
  color: 'var(--text)',
};

export default function Overview() {
  const { data: summary } = useApi<PortfolioSummary>('/api/portfolio/summary', 10000);
  const { data: positions } = useApi<Position[]>('/api/portfolio/positions', 10000);
  const { data: trades } = useApi<Trade[]>('/api/portfolio/trades?limit=20', 10000);
  const { data: metrics } = useApi<Metrics>('/api/portfolio/metrics', 30000);
  const { data: equity } = useApi<EquityPoint[]>('/api/portfolio/equity', 30000);

  const s = summary ?? { bankroll: 0, peak_bankroll: 0, open_position_count: 0, total_exposure: 0, unrealized_pnl: 0, total_return_pct: 0, max_drawdown_pct: 0 };
  const m = metrics ?? { total_trades: 0, wins: 0, losses: 0, win_rate: 0, total_pnl: 0, total_return_pct: 0, avg_edge: 0, avg_ev: 0, calibration_error: 0, avg_pnl_per_trade: 0 };

  const sparkline = (equity ?? []).map(e => e.bankroll);

  const calibrationData = (trades ?? [])
    .filter(t => t.status === 'closed')
    .map(t => ({
      predicted: +(t.p_model * 100).toFixed(0),
      actual: (t.realized_pnl ?? 0) > 0 ? 100 : 0,
    }));

  const returnDir = m.total_return_pct > 0 ? 'up' as const : m.total_return_pct < 0 ? 'down' as const : 'flat' as const;
  const returnColor = m.total_return_pct >= 0 ? 'var(--positive)' : 'var(--negative)';

  return (
    <div style={{ padding: 'var(--gap-lg)', display: 'flex', flexDirection: 'column', gap: 'var(--gap-lg)' }}>

      {/* KPI Row */}
      <div style={{ display: 'flex', gap: 'var(--gap-md)', flexWrap: 'wrap' }}>
        <KpiCard
          label="Bankroll" value={fmtUsd(s.bankroll)}
          delta={fmtPct(s.total_return_pct)} deltaColor={returnColor}
          deltaDirection={returnDir} context="all-time"
          icon={<IconWallet />} sparklineData={sparkline}
          animDelay={0}
        />
        <KpiCard
          label="Total Return" value={fmtPct(m.total_return_pct)}
          delta={fmtUsd(m.total_pnl)} deltaColor={returnColor}
          deltaDirection={returnDir} context="realized"
          icon={<IconTrend />}
          animDelay={60}
        />
        <KpiCard
          label="Open Positions" value={String(s.open_position_count)}
          delta={fmtUsd(s.total_exposure)} context="exposure"
          icon={<IconLayers />}
          animDelay={120}
        />
        <KpiCard
          label="Max Drawdown" value={`${fmt(s.max_drawdown_pct)}%`}
          delta={`${fmt(s.max_drawdown_pct / 20 * 100, 0)}% of limit`}
          deltaColor="var(--warning)" context="20% cap"
          icon={<IconShield />}
          gauge={{ value: s.max_drawdown_pct, max: 20 }}
          animDelay={180}
        />
        <KpiCard
          label="Total Trades" value={String(m.total_trades)}
          delta={`${fmt(m.win_rate)}%`} deltaColor="var(--positive)"
          deltaDirection="up" context="win rate"
          icon={<IconActivity />}
          animDelay={240}
        />
      </div>

      {/* 3-column panels */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 'var(--gap-md)', minHeight: 340 }}>

        {/* Live Opportunities */}
        <Panel title="Live Opportunities" meta={`${(trades ?? []).filter(t => t.status === 'filled').length} active`} animDelay={300}>
          {(trades ?? []).filter(t => t.status === 'filled').length === 0 ? (
            <Empty>No active opportunities</Empty>
          ) : (
            (trades ?? []).filter(t => t.status === 'filled').map((t, i) => (
              <div key={i} style={{
                padding: '10px 12px', marginBottom: 8,
                borderRadius: 'var(--radius-sm)',
                background: 'var(--panel2)',
              }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                  <span style={{ fontSize: '0.82rem', fontWeight: 600, color: 'var(--text)' }}>
                    {t.title || t.market_id}
                  </span>
                  <SidePill side={t.side} price={t.price} />
                </div>
                <div style={{ display: 'flex', gap: 12, fontSize: '0.72rem', color: 'var(--muted)', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}>
                  <span>Edge {fmtPct(t.edge * 100)}</span>
                  <span>EV {fmt(t.net_ev, 4)}</span>
                  <span style={{ color: 'var(--faint)' }}>{'\u00d7'}{t.quantity}</span>
                </div>
              </div>
            ))
          )}
        </Panel>

        {/* Open Positions */}
        <Panel title="Open Positions" meta={fmtUsd(s.total_exposure)} animDelay={360}>
          {(positions ?? []).length === 0 ? (
            <Empty>No open positions</Empty>
          ) : (
            <>
              {/* Table header */}
              <div style={{
                display: 'grid', gridTemplateColumns: '1fr auto auto auto',
                gap: 8, padding: '0 0 8px', borderBottom: '1px solid var(--hairline-soft)',
                marginBottom: 6,
              }}>
                <span className="label">Market</span>
                <span className="label" style={{ textAlign: 'right' }}>Side</span>
                <span className="label" style={{ textAlign: 'right' }}>Entry/Now</span>
                <span className="label" style={{ textAlign: 'right' }}>PnL</span>
              </div>
              {(positions ?? []).map((p, i) => (
                <div key={i} style={{
                  display: 'grid', gridTemplateColumns: '1fr auto auto auto',
                  gap: 8, padding: '8px 0',
                  borderBottom: '1px solid var(--hairline-soft)',
                  fontSize: '0.78rem',
                  transition: 'background 0.1s',
                }}
                onMouseEnter={e => (e.currentTarget.style.background = 'var(--panel2)')}
                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                >
                  <span style={{ color: 'var(--text)', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {p.title || p.market_id}
                  </span>
                  <SidePill side={p.side} />
                  <span style={{ fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums', color: 'var(--muted)', textAlign: 'right' }}>
                    {p.entry_price}{'\u00a2'}{'\u2192'}{p.current_price}{'\u00a2'}
                  </span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums', color: pnlColor(p.unrealized_pnl), textAlign: 'right', fontWeight: 500 }}>
                    {fmtUsd(p.unrealized_pnl)}
                  </span>
                </div>
              ))}
            </>
          )}
        </Panel>

        {/* Activity Log */}
        <Panel title="Activity" meta={`${(trades ?? []).length} trades`} animDelay={420}>
          {(trades ?? []).length === 0 ? (
            <Empty>No activity yet</Empty>
          ) : (
            (trades ?? []).slice(0, 15).map((t, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 10,
                padding: '8px 0', borderBottom: '1px solid var(--hairline-soft)',
              }}>
                <div style={{
                  width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                  background: t.status === 'closed'
                    ? ((t.realized_pnl ?? 0) > 0 ? 'var(--positive)' : 'var(--negative)')
                    : 'var(--accent)',
                }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.78rem', color: 'var(--text)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {t.status === 'closed' ? 'Closed' : 'Opened'} {t.side.toUpperCase()} {t.title || t.market_id}
                  </div>
                  <div style={{ fontSize: '0.68rem', color: 'var(--faint)', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}>
                    {t.price}{'\u00a2'} {'\u00d7'}{t.quantity} {t.realized_pnl != null ? `\u2192 ${fmtUsd(t.realized_pnl)}` : ''}
                  </div>
                </div>
                <div style={{ fontSize: '0.65rem', color: 'var(--faint)', fontFamily: 'var(--font-mono)', flexShrink: 0 }}>
                  {new Date(t.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                </div>
              </div>
            ))
          )}
        </Panel>
      </div>

      {/* Bottom charts row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--gap-md)', height: 280 }}>
        <Panel title="Equity Curve" animDelay={480}>
          {(equity ?? []).length <= 1 ? (
            <Empty>No trade history yet</Empty>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={equity ?? []} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
                <defs>
                  <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#45d6bf" stopOpacity={0.25} />
                    <stop offset="100%" stopColor="#45d6bf" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid horizontal vertical={false} stroke="rgba(255,255,255,0.04)" />
                <XAxis dataKey="timestamp" hide />
                <YAxis
                  domain={['auto', 'auto']}
                  tick={{ fontSize: 10, fill: '#565c67', fontFamily: 'IBM Plex Mono' }}
                  axisLine={false} tickLine={false} width={48}
                />
                <Tooltip
                  contentStyle={tooltipStyle}
                  labelStyle={{ color: '#565c67' }}
                  formatter={(v) => [`$${Number(v ?? 0).toFixed(2)}`, 'Bankroll']}
                />
                <Area type="monotone" dataKey="bankroll" stroke="#45d6bf" strokeWidth={1.5} fill="url(#eqGrad)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </Panel>

        <Panel title="Model Calibration" animDelay={540}>
          {calibrationData.length === 0 ? (
            <Empty>No closed trades for calibration</Empty>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 8, right: 8, bottom: 20, left: 8 }}>
                <CartesianGrid horizontal vertical={false} stroke="rgba(255,255,255,0.04)" />
                <XAxis
                  type="number" dataKey="predicted" name="Predicted" domain={[0, 100]}
                  tick={{ fontSize: 10, fill: '#565c67', fontFamily: 'IBM Plex Mono' }}
                  axisLine={false} tickLine={false}
                  label={{ value: 'Predicted %', position: 'bottom', fontSize: 10, fill: '#565c67', fontFamily: 'IBM Plex Mono' }}
                />
                <YAxis
                  type="number" dataKey="actual" name="Actual" domain={[0, 100]}
                  tick={{ fontSize: 10, fill: '#565c67', fontFamily: 'IBM Plex Mono' }}
                  axisLine={false} tickLine={false} width={32}
                />
                <ReferenceLine segment={[{ x: 0, y: 0 }, { x: 100, y: 100 }]} stroke="#565c67" strokeDasharray="4 4" />
                <Tooltip contentStyle={tooltipStyle} />
                <Scatter data={calibrationData} fill="#45d6bf" opacity={0.7} />
              </ScatterChart>
            </ResponsiveContainer>
          )}
        </Panel>
      </div>
    </div>
  );
}

/* Shared small components */

function Empty({ children }: { children: string }) {
  return (
    <div style={{ color: 'var(--faint)', fontSize: '0.82rem', textAlign: 'center', padding: 'var(--gap-xl) 0' }}>
      {children}
    </div>
  );
}

function SidePill({ side, price }: { side: string; price?: number }) {
  const yes = side.toLowerCase() === 'yes';
  const bg = yes ? 'var(--positive-dim)' : 'var(--negative-dim)';
  const color = yes ? 'var(--positive)' : 'var(--negative)';
  const border = yes ? 'rgba(63,184,106,0.2)' : 'rgba(229,85,107,0.2)';
  return (
    <span style={{
      padding: '2px 8px', borderRadius: 4,
      fontSize: '0.7rem', fontFamily: 'var(--font-mono)', fontWeight: 500,
      fontVariantNumeric: 'tabular-nums',
      background: bg, color, border: `1px solid ${border}`,
    }}>
      {side.toUpperCase()}{price != null ? ` ${price}\u00a2` : ''}
    </span>
  );
}
