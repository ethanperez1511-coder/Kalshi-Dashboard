import type { CSSProperties } from 'react';
import { useMemo, useState } from 'react';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import Panel from '../components/Panel';
import { useApi } from '../hooks/useApi';
import type { BacktestReport, BacktestRun, BacktestRunResult } from '../types/api';

const today = new Date().toISOString().slice(0, 10);
const defaultStart = new Date(Date.now() - 1000 * 60 * 60 * 24 * 60).toISOString().slice(0, 10);

const tooltipStyle = {
  background: 'var(--panel2)',
  border: '1px solid var(--hairline)',
  borderRadius: 8,
  fontFamily: 'var(--font-mono)',
  fontSize: '0.72rem',
  color: 'var(--text)',
};

export default function Backtest() {
  const { data: runs, refetch } = useApi<BacktestRun[]>('/api/backtest?limit=20', 20000);
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({
    start_date: defaultStart,
    end_date: today,
    initial_bankroll: '100',
    category_filter: '',
  });

  const activeRunId = selectedRun ?? runs?.[0]?.id ?? null;
  const { data: report } = useApi<BacktestReport>(
    activeRunId ? `/api/backtest/${activeRunId}` : '',
    activeRunId ? 30000 : undefined,
  );

  const runBacktest = async () => {
    setRunning(true);
    setError(null);
    try {
      const response = await fetch('/api/backtest/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start_date: new Date(form.start_date).toISOString(),
          end_date: new Date(form.end_date).toISOString(),
          initial_bankroll: Number(form.initial_bankroll),
          category_filter: form.category_filter || null,
        }),
      });
      if (!response.ok) throw new Error(`Failed to run backtest (${response.status})`);
      const result = (await response.json()) as BacktestRunResult;
      setSelectedRun(result.run_id);
      refetch();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const reportStats = useMemo(() => {
    if (!report) return [];
    return [
      { label: 'Trades', value: String(report.total_trades), color: 'var(--text)' },
      { label: 'Win Rate', value: `${report.win_rate.toFixed(1)}%`, color: 'var(--positive)' },
      { label: 'Return', value: `${report.total_return_pct.toFixed(2)}%`, color: report.total_return_pct >= 0 ? 'var(--positive)' : 'var(--negative)' },
      { label: 'Max DD', value: `${report.max_drawdown_pct.toFixed(2)}%`, color: 'var(--warning)' },
    ];
  }, [report]);

  return (
    <div style={{ padding: 'var(--gap-lg)', display: 'grid', gridTemplateColumns: '370px 1fr', gap: 'var(--gap-md)' }}>
      <Panel title="Run Backtest" animDelay={60} style={{ minHeight: 640 }}>
        <div style={{ display: 'grid', gap: 'var(--gap-md)' }}>
          <FormField label="Start Date">
            <input type="date" value={form.start_date} onChange={(e) => setForm((p) => ({ ...p, start_date: e.target.value }))} style={inputStyle} />
          </FormField>
          <FormField label="End Date">
            <input type="date" value={form.end_date} onChange={(e) => setForm((p) => ({ ...p, end_date: e.target.value }))} style={inputStyle} />
          </FormField>
          <FormField label="Initial Bankroll">
            <input type="number" value={form.initial_bankroll} min="1" onChange={(e) => setForm((p) => ({ ...p, initial_bankroll: e.target.value }))} style={inputStyle} />
          </FormField>
          <FormField label="Category Filter (optional)">
            <input type="text" value={form.category_filter} placeholder="sports, economics..." onChange={(e) => setForm((p) => ({ ...p, category_filter: e.target.value }))} style={inputStyle} />
          </FormField>
          <button
            onClick={runBacktest}
            disabled={running}
            style={{
              padding: '10px 12px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--accent-border)',
              color: 'var(--accent)',
              background: 'var(--accent-dim)',
              fontFamily: 'var(--font-sans)',
              fontWeight: 600,
              textTransform: 'uppercase',
              letterSpacing: '0.08em',
              fontSize: '0.75rem',
              cursor: running ? 'progress' : 'pointer',
              opacity: running ? 0.6 : 1,
              transition: 'opacity 0.15s',
            }}
          >
            {running ? 'Running\u2026' : 'Run Backtest'}
          </button>
          {error && <div style={{ color: 'var(--negative)', fontSize: '0.75rem' }}>{error}</div>}
        </div>

        <div style={{ marginTop: 'var(--gap-lg)' }}>
          <span className="label" style={{ display: 'block', marginBottom: 8 }}>Recent Runs</span>
          <div style={{ display: 'grid', gap: 6 }}>
            {(runs ?? []).map((run) => {
              const active = run.id === activeRunId;
              return (
                <button
                  key={run.id}
                  onClick={() => setSelectedRun(run.id)}
                  style={{
                    width: '100%',
                    textAlign: 'left',
                    border: `1px solid ${active ? 'var(--accent-border)' : 'var(--hairline)'}`,
                    background: active ? 'var(--panel2)' : 'transparent',
                    borderRadius: 'var(--radius-sm)',
                    padding: '8px 10px',
                    cursor: 'pointer',
                    transition: 'background 0.1s',
                  }}
                  onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--panel2)'; }}
                  onMouseLeave={e => { if (!active) e.currentTarget.style.background = 'transparent'; }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: 'var(--font-mono)', fontSize: '0.72rem', fontVariantNumeric: 'tabular-nums' }}>
                    <span style={{ color: 'var(--text)' }}>#{run.id}</span>
                    <span style={{ color: run.total_pnl >= 0 ? 'var(--positive)' : 'var(--negative)' }}>
                      ${run.total_pnl.toFixed(2)}
                    </span>
                  </div>
                  <div style={{ marginTop: 4, color: 'var(--faint)', fontSize: '0.68rem', fontFamily: 'var(--font-mono)' }}>
                    {new Date(run.created_at).toLocaleString()}
                  </div>
                </button>
              );
            })}
            {(runs ?? []).length === 0 && <div style={{ color: 'var(--faint)', fontSize: '0.8rem' }}>No backtests yet.</div>}
          </div>
        </div>
      </Panel>

      <Panel title="Backtest Report" animDelay={120} style={{ minHeight: 640 }}>
        {!report ? (
          <div style={{ color: 'var(--faint)', fontSize: '0.82rem' }}>Select or run a backtest to view report data.</div>
        ) : (
          <div style={{ display: 'grid', gap: 'var(--gap-md)' }}>
            {/* Stats row */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8 }}>
              {reportStats.map((stat) => (
                <div key={stat.label} style={{
                  background: 'var(--panel2)', borderRadius: 'var(--radius-sm)',
                  padding: '10px 12px', border: '1px solid var(--hairline)',
                }}>
                  <span className="label">{stat.label}</span>
                  <div style={{
                    color: stat.color, fontFamily: 'var(--font-mono)',
                    fontVariantNumeric: 'tabular-nums',
                    fontSize: '1.15rem', fontWeight: 600, marginTop: 4,
                  }}>{stat.value}</div>
                </div>
              ))}
            </div>

            {/* Equity chart */}
            <div style={{ height: 280, borderRadius: 'var(--radius-sm)', background: 'var(--panel2)', padding: 14, border: '1px solid var(--hairline)' }}>
              <span className="label" style={{ display: 'block', marginBottom: 8 }}>Equity Curve</span>
              <ResponsiveContainer width="100%" height="90%">
                <AreaChart data={report.equity_curve}>
                  <defs>
                    <linearGradient id="btEqGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#45d6bf" stopOpacity={0.25} />
                      <stop offset="100%" stopColor="#45d6bf" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid horizontal vertical={false} stroke="rgba(255,255,255,0.04)" />
                  <XAxis dataKey="timestamp" hide />
                  <YAxis
                    tick={{ fontSize: 10, fill: '#565c67', fontFamily: 'IBM Plex Mono' }}
                    axisLine={false} tickLine={false} width={48}
                  />
                  <Tooltip
                    contentStyle={tooltipStyle}
                    formatter={(v) => [`$${Number(v ?? 0).toFixed(2)}`, 'Bankroll']}
                  />
                  <Area dataKey="bankroll" stroke="#45d6bf" strokeWidth={1.5} fill="url(#btEqGrad)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Limitations */}
            <div style={{ background: 'var(--panel2)', borderRadius: 'var(--radius-sm)', padding: 14, border: '1px solid var(--hairline)' }}>
              <span className="label" style={{ display: 'block', marginBottom: 8 }}>Report Limitations</span>
              <ul style={{ marginLeft: 14, display: 'grid', gap: 6 }}>
                {report.limitations.map((item) => (
                  <li key={item} style={{ color: 'var(--muted)', fontSize: '0.8rem' }}>{item}</li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'grid', gap: 6 }}>
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

const inputStyle: CSSProperties = {
  width: '100%',
  border: '1px solid var(--hairline)',
  background: 'var(--panel2)',
  color: 'var(--text)',
  borderRadius: 'var(--radius-sm)',
  padding: '8px 10px',
  fontFamily: 'var(--font-mono)',
  fontVariantNumeric: 'tabular-nums',
  fontSize: '0.78rem',
};
