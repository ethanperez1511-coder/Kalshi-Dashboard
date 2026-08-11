import Panel from './Panel';
import { useApi } from '../hooks/useApi';
import type { QuotaStatus } from '../types/api';

const API = 'http://127.0.0.1:8000';

function fmtDays(days: number | null): string {
  if (days === null) return '—';
  if (days > 365) return '>1y';
  return `${days.toFixed(1)}d`;
}

/**
 * Odds-provider quota burn.
 *
 * This is the one resource whose exhaustion is invisible from the outside: the
 * pipeline keeps cycling, the sports model just silently stops producing
 * estimates for the rest of the month. The projection is the point of the
 * widget — knowing on the 5th that the month ends at 3x cap.
 */
export default function QuotaCard({ animDelay = 0 }: { animDelay?: number }) {
  const { data, loading, error } = useApi<QuotaStatus>(`${API}/api/quota`, 60_000);

  if (loading) return <Panel title="Odds quota" animDelay={animDelay}><div style={{ padding: 'var(--gap-md)', color: 'var(--muted)' }}>Loading…</div></Panel>;
  if (error || !data) return <Panel title="Odds quota" animDelay={animDelay}><div style={{ padding: 'var(--gap-md)', color: 'var(--negative)' }}>Unavailable: {error ?? 'no data'}</div></Panel>;

  const usedPct = data.cap > 0 ? Math.min(100, (data.used / data.cap) * 100) : 0;
  const projectedPct = data.cap > 0 ? Math.min(100, (data.projected_month_end / data.cap) * 100) : 0;
  const barColor = data.projected_overrun ? 'var(--negative)' : 'var(--accent)';

  return (
    <Panel title="Odds quota" meta={data.month} animDelay={animDelay}>
      <div style={{ padding: 'var(--gap-md)', display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
          <span style={{ fontSize: 28, fontWeight: 600 }}>{data.used}</span>
          <span style={{ color: 'var(--muted)' }}>/ {data.cap} requests</span>
        </div>

        {/* Spent so far, with the month-end projection marked behind it. */}
        <div style={{ position: 'relative', height: 8, borderRadius: 4, background: 'var(--hairline)', overflow: 'hidden' }}>
          <div style={{ position: 'absolute', inset: 0, width: `${projectedPct}%`, background: barColor, opacity: 0.25 }} />
          <div style={{ position: 'absolute', inset: 0, width: `${usedPct}%`, background: barColor }} />
        </div>

        <dl style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 16px', margin: 0, fontSize: 13 }}>
          <div><dt style={{ color: 'var(--muted)' }}>Burn rate</dt><dd style={{ margin: 0 }}>{data.burn_per_day.toFixed(1)}/day</dd></div>
          <div><dt style={{ color: 'var(--muted)' }}>Projected month end</dt><dd style={{ margin: 0, color: data.projected_overrun ? 'var(--negative)' : undefined }}>{Math.round(data.projected_month_end)}</dd></div>
          <div><dt style={{ color: 'var(--muted)' }}>Runs dry in</dt><dd style={{ margin: 0 }}>{fmtDays(data.days_to_exhaustion)}</dd></div>
          <div><dt style={{ color: 'var(--muted)' }}>Free fallback</dt><dd style={{ margin: 0 }}>{data.fallback_enabled ? 'ESPN on' : 'off'}</dd></div>
        </dl>

        {data.projected_overrun && (
          <div style={{ fontSize: 12, color: 'var(--negative)' }}>
            On pace to exhaust the monthly cap — sports coverage will go dark.
          </div>
        )}

        {data.cache.length > 0 && (
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            {data.cache
              .slice()
              .sort((a, b) => a.sport_key.localeCompare(b.sport_key))
              .map((c) => `${c.sport_key} ${Math.round(c.age_minutes)}m (${c.source})`)
              .join(' · ')}
          </div>
        )}
      </div>
    </Panel>
  );
}
