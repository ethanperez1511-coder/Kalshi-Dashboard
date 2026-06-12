import { useMemo, useState } from 'react';
import Panel from '../components/Panel';
import { useApi } from '../hooks/useApi';
import type { Market, PriceSnapshot, Trade } from '../types/api';

function statusColor(status: string) {
  if (status === 'open') return 'var(--positive)';
  if (status === 'settled' || status === 'closed') return 'var(--negative)';
  return 'var(--warning)';
}

export default function Markets() {
  const { data: markets } = useApi<Market[]>('/api/markets', 45000);
  const { data: trades } = useApi<Trade[]>('/api/portfolio/trades?limit=500', 30000);

  const [categoryFilter, setCategoryFilter] = useState<string>('all');
  const [selectedMarket, setSelectedMarket] = useState<string | null>(null);

  const filteredMarkets = useMemo(() => {
    const list = markets ?? [];
    if (categoryFilter === 'all') return list;
    return list.filter((m) => m.category === categoryFilter);
  }, [markets, categoryFilter]);

  const selectedId = selectedMarket ?? filteredMarkets[0]?.market_id ?? '';
  const { data: prices } = useApi<PriceSnapshot[]>(
    selectedId ? `/api/markets/${selectedId}/prices?limit=120` : '',
    selectedId ? 15000 : undefined,
  );

  const categories = useMemo(() => {
    return ['all', ...new Set((markets ?? []).map((m) => m.category))];
  }, [markets]);

  const pModelByMarket = useMemo(() => {
    const lookup = new Map<string, number>();
    for (const trade of trades ?? []) {
      if (!lookup.has(trade.market_id)) {
        lookup.set(trade.market_id, trade.p_model * 100);
      }
    }
    return lookup;
  }, [trades]);

  const latestPrice = prices?.at(-1);

  return (
    <div style={{ padding: 'var(--gap-lg)', display: 'grid', gridTemplateColumns: '1.3fr 1fr', gap: 'var(--gap-md)' }}>
      <Panel title="Market Universe" meta={`${filteredMarkets.length} markets`} animDelay={60} style={{ minHeight: 640 }}>
        {/* Category filter pills */}
        <div style={{ display: 'flex', gap: 'var(--gap-xs)', marginBottom: 'var(--gap-md)', flexWrap: 'wrap' }}>
          {categories.map((category) => (
            <button
              key={category}
              onClick={() => setCategoryFilter(category)}
              style={{
                border: `1px solid ${categoryFilter === category ? 'var(--accent-border)' : 'var(--hairline)'}`,
                background: categoryFilter === category ? 'var(--accent-dim)' : 'transparent',
                color: categoryFilter === category ? 'var(--accent)' : 'var(--muted)',
                padding: '5px 10px',
                borderRadius: 'var(--radius-sm)',
                fontFamily: 'var(--font-sans)',
                fontSize: '0.72rem',
                fontWeight: 500,
                letterSpacing: '0.08em',
                textTransform: 'uppercase',
                cursor: 'pointer',
              }}
            >
              {category}
            </button>
          ))}
        </div>

        {/* Market list */}
        <div style={{ display: 'grid', gap: 6 }}>
          {filteredMarkets.length === 0 && (
            <div style={{ color: 'var(--faint)', fontSize: '0.82rem', padding: 'var(--gap-lg) 0' }}>
              No markets available yet.
            </div>
          )}
          {filteredMarkets.map((market) => {
            const selected = selectedId === market.market_id;
            return (
              <button
                key={market.market_id}
                onClick={() => setSelectedMarket(market.market_id)}
                style={{
                  width: '100%',
                  textAlign: 'left',
                  border: `1px solid ${selected ? 'var(--accent-border)' : 'var(--hairline)'}`,
                  background: selected ? 'var(--panel2)' : 'transparent',
                  borderRadius: 'var(--radius-sm)',
                  padding: '10px 12px',
                  cursor: 'pointer',
                  transition: 'background 0.1s',
                }}
                onMouseEnter={e => { if (!selected) e.currentTarget.style.background = 'var(--panel2)'; }}
                onMouseLeave={e => { if (!selected) e.currentTarget.style.background = 'transparent'; }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                  <div style={{ color: 'var(--text)', fontSize: '0.82rem', fontWeight: 500 }}>{market.title}</div>
                  <span style={{
                    color: statusColor(market.status),
                    fontFamily: 'var(--font-mono)', fontSize: '0.68rem',
                    fontVariantNumeric: 'tabular-nums',
                  }}>
                    {market.status.toUpperCase()}
                  </span>
                </div>
                <div style={{ marginTop: 6, display: 'flex', gap: 10, color: 'var(--faint)', fontSize: '0.7rem', fontFamily: 'var(--font-mono)' }}>
                  <span>{market.category}</span>
                  <span>{new Date(market.close_date).toLocaleDateString()}</span>
                </div>
              </button>
            );
          })}
        </div>
      </Panel>

      <Panel title="Pricing + Model View" animDelay={120} style={{ minHeight: 640 }}>
        {!selectedId ? (
          <div style={{ color: 'var(--faint)', fontSize: '0.82rem' }}>Select a market to view details.</div>
        ) : (
          <div style={{ display: 'grid', gap: 'var(--gap-md)' }}>
            {/* Latest snapshot */}
            <div style={{ padding: 14, borderRadius: 'var(--radius-sm)', background: 'var(--panel2)' }}>
              <span className="label">Latest Snapshot</span>
              <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <div>
                  <div className="label" style={{ marginBottom: 4 }}>YES Bid / Ask</div>
                  <div style={{
                    color: 'var(--text)', fontSize: '1.1rem',
                    fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums',
                  }}>
                    {latestPrice ? `${latestPrice.yes_bid}\u00a2 / ${latestPrice.yes_ask}\u00a2` : '\u2013'}
                  </div>
                </div>
                <div>
                  <div className="label" style={{ marginBottom: 4 }}>Last / Volume</div>
                  <div style={{
                    color: 'var(--text)', fontSize: '1.1rem',
                    fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums',
                  }}>
                    {latestPrice ? `${latestPrice.last_price}\u00a2 / ${latestPrice.volume}` : '\u2013'}
                  </div>
                </div>
              </div>
            </div>

            {/* Model vs Market */}
            <div style={{ padding: 14, borderRadius: 'var(--radius-sm)', background: 'var(--panel2)' }}>
              <span className="label">Model vs Market</span>
              <div style={{ marginTop: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'end' }}>
                <div>
                  <div className="label" style={{ marginBottom: 4 }}>Model Probability</div>
                  <div style={{
                    color: 'var(--accent)', fontSize: '1.2rem', fontWeight: 600,
                    fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums',
                  }}>
                    {pModelByMarket.has(selectedId) ? `${pModelByMarket.get(selectedId)?.toFixed(1)}%` : 'No signal'}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div className="label" style={{ marginBottom: 4 }}>Market Implied</div>
                  <div style={{
                    color: 'var(--text)', fontSize: '1.2rem', fontWeight: 600,
                    fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums',
                  }}>
                    {latestPrice ? `${latestPrice.last_price}%` : '\u2013'}
                  </div>
                </div>
              </div>
            </div>

            {/* Price history */}
            <div style={{ padding: 14, borderRadius: 'var(--radius-sm)', background: 'var(--panel2)' }}>
              <span className="label" style={{ display: 'block', marginBottom: 10 }}>Recent Price History</span>
              {/* Column headers */}
              <div style={{
                display: 'flex', justifyContent: 'space-between',
                padding: '0 0 6px', borderBottom: '1px solid var(--hairline-soft)',
                marginBottom: 4,
              }}>
                <span className="label">Time</span>
                <span className="label">Price (Bid / Ask)</span>
              </div>
              <div style={{ display: 'grid', gap: 4 }}>
                {(prices ?? []).slice(-8).reverse().map((snapshot) => (
                  <div key={snapshot.timestamp} style={{
                    display: 'flex', justifyContent: 'space-between', fontSize: '0.73rem',
                    padding: '4px 0',
                  }}>
                    <span style={{ color: 'var(--faint)', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}>
                      {new Date(snapshot.timestamp).toLocaleTimeString()}
                    </span>
                    <span style={{ color: 'var(--text)', fontFamily: 'var(--font-mono)', fontVariantNumeric: 'tabular-nums' }}>
                      {snapshot.last_price}{'\u00a2'} ({snapshot.yes_bid} / {snapshot.yes_ask})
                    </span>
                  </div>
                ))}
                {(prices ?? []).length === 0 && <div style={{ color: 'var(--faint)', fontSize: '0.8rem' }}>No price history.</div>}
              </div>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
