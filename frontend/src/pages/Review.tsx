import { useState } from 'react';
import Panel from '../components/Panel';
import { useApi } from '../hooks/useApi';
import type { MarketMatch } from '../types/api';

const API = 'http://127.0.0.1:8000';

/**
 * Review queue for uncertain Kalshi↔Polymarket mappings.
 *
 * The model refuses to price anything it lands here, so this page is the only
 * way an uncertain pair ever becomes tradeable. Each decision is permanent:
 * approve once and the mapping is reused forever, block once and the pair is
 * never matched again regardless of how similar the titles look.
 */
export default function Review() {
  const { data, loading, error, refetch } = useApi<MarketMatch[]>(`${API}/api/matches/pending`, 30_000);
  const [busy, setBusy] = useState<number | null>(null);

  async function decide(id: number, action: 'approve' | 'block') {
    setBusy(id);
    try {
      await fetch(`${API}/api/matches/${id}/${action}`, { method: 'POST' });
      await refetch();
    } finally {
      setBusy(null);
    }
  }

  const matches = data ?? [];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--gap-md)' }}>
      <Panel title="Match review queue" meta={`${matches.length} pending`}>
        <div style={{ padding: 'var(--gap-md)', color: 'var(--muted)', fontSize: 13 }}>
          These Kalshi markets found a plausible Polymarket counterpart, but the
          entity check could not confirm they resolve on the same event. Until you
          decide, the model produces no estimate for them.
        </div>
      </Panel>

      {loading && <Panel title="Loading"><div style={{ padding: 'var(--gap-md)' }}>Loading…</div></Panel>}
      {error && <Panel title="Error"><div style={{ padding: 'var(--gap-md)', color: 'var(--negative)' }}>{error}</div></Panel>}

      {!loading && !error && matches.length === 0 && (
        <Panel title="Nothing to review">
          <div style={{ padding: 'var(--gap-md)', color: 'var(--muted)' }}>
            No uncertain matches queued.
          </div>
        </Panel>
      )}

      {matches.map((m, i) => (
        <Panel
          key={m.id}
          title={m.kalshi_market_id}
          meta={`${m.verdict ?? 'uncertain'} · similarity ${m.similarity.toFixed(2)}`}
          animDelay={i * 40}
        >
          <div style={{ padding: 'var(--gap-md)', display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--gap-md)' }}>
              <div>
                <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 4 }}>Kalshi</div>
                <div>{m.kalshi_title ?? '—'}</div>
              </div>
              <div>
                <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 4 }}>Polymarket</div>
                <div>{m.poly_question ?? '—'}</div>
                <div style={{ color: 'var(--muted)', fontSize: 11, marginTop: 4 }}>{m.poly_condition_id}</div>
              </div>
            </div>

            <div style={{
              padding: '8px 10px', borderRadius: 'var(--radius-sm, 6px)',
              background: 'var(--hairline)', fontSize: 13,
            }}>
              <strong>Why it stopped:</strong> {m.reason ?? 'unspecified'}
            </div>

            <div style={{ display: 'flex', gap: 8 }}>
              <button
                onClick={() => decide(m.id, 'approve')}
                disabled={busy === m.id}
                style={{
                  padding: '6px 14px', borderRadius: 6, cursor: 'pointer',
                  border: '1px solid var(--hairline)', background: 'var(--panel)',
                  color: 'var(--positive)',
                }}
              >
                Approve — same event
              </button>
              <button
                onClick={() => decide(m.id, 'block')}
                disabled={busy === m.id}
                style={{
                  padding: '6px 14px', borderRadius: 6, cursor: 'pointer',
                  border: '1px solid var(--hairline)', background: 'var(--panel)',
                  color: 'var(--negative)',
                }}
              >
                Block — different event
              </button>
            </div>
          </div>
        </Panel>
      ))}
    </div>
  );
}
