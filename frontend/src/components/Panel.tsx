import type { CSSProperties, ReactNode } from 'react';

interface Props {
  title: string;
  meta?: string;
  children: ReactNode;
  style?: CSSProperties;
  animDelay?: number;
}

export default function Panel({ title, meta, children, style, animDelay = 0 }: Props) {
  return (
    <div
      className="animate-in"
      style={{
        background: 'var(--panel)',
        border: '1px solid var(--hairline)',
        borderRadius: 'var(--radius-md)',
        display: 'flex', flexDirection: 'column',
        overflow: 'hidden',
        animationDelay: `${animDelay}ms`,
        ...style,
      }}
    >
      <div style={{
        padding: '12px var(--gap-md)',
        borderBottom: '1px solid var(--hairline)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* 3px accent tick */}
          <div style={{
            width: 3, height: 14, borderRadius: 2,
            background: 'var(--accent)',
          }} />
          <span className="label" style={{ fontSize: 11 }}>{title}</span>
        </div>
        {meta && (
          <span style={{ color: 'var(--faint)', fontSize: '0.72rem', fontFamily: 'var(--font-mono)' }}>
            {meta}
          </span>
        )}
      </div>
      <div style={{ padding: 'var(--gap-md)', flex: 1, overflow: 'auto' }}>
        {children}
      </div>
    </div>
  );
}
