import type { ReactNode } from 'react';

interface Props {
  label: string;
  value: string;
  delta?: string;
  deltaColor?: string;
  deltaDirection?: 'up' | 'down' | 'flat';
  context?: string;
  icon?: ReactNode;
  gauge?: { value: number; max: number };
  sparklineData?: number[];
  animDelay?: number;
}

function MiniSparkline({ data }: { data: number[] }) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const w = 64;
  const h = 20;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * h;
    return `${x},${y}`;
  }).join(' ');
  return (
    <svg width={w} height={h} style={{ display: 'block', marginTop: 6 }}>
      <polyline
        points={points}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function GaugeBar({ value, max }: { value: number; max: number }) {
  const pct = Math.min(Math.abs(value) / max, 1) * 100;
  const danger = pct > 60;
  const color = danger ? 'var(--negative)' : 'var(--warning)';
  return (
    <div style={{
      marginTop: 8, height: 4, borderRadius: 2,
      background: 'var(--hairline)',
      overflow: 'hidden',
    }}>
      <div style={{
        height: '100%', borderRadius: 2,
        width: `${pct}%`,
        background: color,
        transition: 'width 0.4s ease',
      }} />
    </div>
  );
}

const ArrowUp = () => (
  <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
    <path d="M5 2L8 6H2L5 2Z" fill="currentColor" />
  </svg>
);

const ArrowDown = () => (
  <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
    <path d="M5 8L2 4H8L5 8Z" fill="currentColor" />
  </svg>
);

export default function KpiCard({
  label, value, delta, deltaColor, deltaDirection, context,
  icon, gauge, sparklineData, animDelay = 0,
}: Props) {
  return (
    <div
      className="animate-in"
      style={{
        flex: 1, minWidth: 160,
        background: 'var(--panel)',
        border: '1px solid var(--hairline)',
        borderRadius: 'var(--radius-md)',
        padding: 'var(--gap-md)',
        animationDelay: `${animDelay}ms`,
      }}
    >
      {/* Header: label + icon */}
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        marginBottom: 10,
      }}>
        <span className="label">{label}</span>
        {icon && <span style={{ color: 'var(--faint)', lineHeight: 0 }}>{icon}</span>}
      </div>

      {/* Value */}
      <div style={{
        fontFamily: 'var(--font-mono)',
        fontVariantNumeric: 'tabular-nums',
        fontSize: 25,
        fontWeight: 600,
        color: 'var(--text)',
        letterSpacing: '-0.5px',
        lineHeight: 1.1,
      }}>
        {value}
      </div>

      {/* Delta row */}
      {delta && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 4,
          marginTop: 6, fontSize: '0.78rem',
        }}>
          {deltaDirection && deltaDirection !== 'flat' && (
            <span style={{ color: deltaColor, lineHeight: 0 }}>
              {deltaDirection === 'up' ? <ArrowUp /> : <ArrowDown />}
            </span>
          )}
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontVariantNumeric: 'tabular-nums',
            color: deltaColor || 'var(--muted)',
            fontWeight: 500,
          }}>
            {delta}
          </span>
          {context && (
            <span style={{ color: 'var(--faint)', fontSize: '0.72rem' }}>{context}</span>
          )}
        </div>
      )}

      {/* Sparkline (Bankroll card) */}
      {sparklineData && sparklineData.length > 1 && (
        <MiniSparkline data={sparklineData} />
      )}

      {/* Gauge bar (Drawdown card) */}
      {gauge && <GaugeBar value={gauge.value} max={gauge.max} />}
    </div>
  );
}
