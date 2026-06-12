import { useState } from 'react';
import Panel from '../components/Panel';

interface TradingSettings {
  mode: 'paper' | 'live';
  bankroll: string;
  edgeThreshold: string;
  maxTradePct: string;
  maxExposurePct: string;
  kalshiApiKey: string;
}

const defaultSettings: TradingSettings = {
  mode: 'paper',
  bankroll: '100',
  edgeThreshold: '5',
  maxTradePct: '3',
  maxExposurePct: '25',
  kalshiApiKey: '',
};

export default function Settings() {
  const [settings, setSettings] = useState<TradingSettings>(() => {
    const raw = localStorage.getItem('kalshi-dashboard-settings');
    if (!raw) return defaultSettings;
    try {
      return JSON.parse(raw) as TradingSettings;
    } catch {
      localStorage.removeItem('kalshi-dashboard-settings');
      return defaultSettings;
    }
  });
  const [savedAt, setSavedAt] = useState<string | null>(null);

  const save = () => {
    localStorage.setItem('kalshi-dashboard-settings', JSON.stringify(settings));
    setSavedAt(new Date().toLocaleTimeString());
  };

  return (
    <div style={{ padding: 'var(--gap-lg)', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--gap-md)' }}>
      <Panel title="Trading Controls" animDelay={60} style={{ minHeight: 540 }}>
        <div style={{ display: 'grid', gap: 'var(--gap-md)' }}>
          {/* Mode toggle */}
          <div>
            <span className="label" style={{ display: 'block', marginBottom: 8 }}>Trading Mode</span>
            <div style={{ display: 'flex', gap: 6 }}>
              {(['paper', 'live'] as const).map((mode) => {
                const active = settings.mode === mode;
                return (
                  <button
                    key={mode}
                    onClick={() => setSettings((p) => ({ ...p, mode }))}
                    style={{
                      padding: '7px 14px',
                      borderRadius: 'var(--radius-sm)',
                      border: `1px solid ${active ? 'var(--accent-border)' : 'var(--hairline)'}`,
                      background: active ? 'var(--accent-dim)' : 'transparent',
                      color: active ? 'var(--accent)' : 'var(--muted)',
                      textTransform: 'uppercase',
                      fontFamily: 'var(--font-sans)',
                      fontWeight: 600,
                      letterSpacing: '0.08em',
                      fontSize: '0.72rem',
                      cursor: 'pointer',
                      transition: 'all 0.15s',
                    }}
                  >
                    {mode}
                  </button>
                );
              })}
            </div>
          </div>

          <SettingField label="Bankroll ($)" value={settings.bankroll} onChange={(v) => setSettings((p) => ({ ...p, bankroll: v }))} />
          <SettingField label="Edge Threshold (%)" value={settings.edgeThreshold} onChange={(v) => setSettings((p) => ({ ...p, edgeThreshold: v }))} />
          <SettingField label="Max Single Trade (% bankroll)" value={settings.maxTradePct} onChange={(v) => setSettings((p) => ({ ...p, maxTradePct: v }))} />
          <SettingField label="Max Total Exposure (%)" value={settings.maxExposurePct} onChange={(v) => setSettings((p) => ({ ...p, maxExposurePct: v }))} />
          <SettingField label="Kalshi API Key" value={settings.kalshiApiKey} onChange={(v) => setSettings((p) => ({ ...p, kalshiApiKey: v }))} />

          <button
            onClick={save}
            style={{
              marginTop: 4,
              padding: '10px 12px',
              borderRadius: 'var(--radius-sm)',
              border: '1px solid var(--accent-border)',
              background: 'var(--accent-dim)',
              color: 'var(--accent)',
              textTransform: 'uppercase',
              fontFamily: 'var(--font-sans)',
              fontWeight: 600,
              letterSpacing: '0.08em',
              fontSize: '0.74rem',
              cursor: 'pointer',
            }}
          >
            Save Local Settings
          </button>

          {savedAt && (
            <div style={{ color: 'var(--positive)', fontSize: '0.76rem', fontFamily: 'var(--font-mono)' }}>
              Saved at {savedAt}
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Risk Rules + Notes" animDelay={120} style={{ minHeight: 540 }}>
        <div style={{ display: 'grid', gap: 'var(--gap-md)' }}>
          <div style={{ background: 'var(--panel2)', borderRadius: 'var(--radius-sm)', padding: 14, border: '1px solid var(--hairline)' }}>
            <span className="label" style={{ display: 'block', marginBottom: 8 }}>Hard Limits (Non-Overridable)</span>
            <ul style={{ marginLeft: 14, display: 'grid', gap: 6 }}>
              <li style={liStyle}>Max single trade: 3% of bankroll</li>
              <li style={liStyle}>Max total exposure: 25% of bankroll</li>
              <li style={liStyle}>Daily loss limit: 5% then pause</li>
              <li style={liStyle}>Drawdown breaker: 20% from peak</li>
            </ul>
          </div>

          <div style={{ background: 'var(--panel2)', borderRadius: 'var(--radius-sm)', padding: 14, border: '1px solid var(--hairline)' }}>
            <span className="label" style={{ display: 'block', marginBottom: 8 }}>Trading Mode Safety</span>
            <p style={{ color: 'var(--muted)', fontSize: '0.82rem', lineHeight: 1.6 }}>
              Paper mode is enforced by default. Live mode should only be enabled after enough paper trades and full model validation.
              The dashboard never implies guaranteed returns and should always be interpreted with uncertainty in mind.
            </p>
          </div>

          <div style={{
            background: 'var(--negative-dim)',
            border: '1px solid rgba(229, 85, 107, 0.2)',
            borderRadius: 'var(--radius-sm)', padding: 14,
          }}>
            <span className="label" style={{ display: 'block', marginBottom: 8, color: 'var(--negative)' }}>Warning</span>
            <p style={{ color: 'var(--muted)', fontSize: '0.8rem', lineHeight: 1.6 }}>
              Backtest performance is not predictive of future outcomes. Use limits first, optimize second.
            </p>
          </div>
        </div>
      </Panel>
    </div>
  );
}

function SettingField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label style={{ display: 'grid', gap: 6 }}>
      <span className="label">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          width: '100%',
          border: '1px solid var(--hairline)',
          background: 'var(--panel2)',
          color: 'var(--text)',
          borderRadius: 'var(--radius-sm)',
          padding: '8px 10px',
          fontFamily: 'var(--font-mono)',
          fontVariantNumeric: 'tabular-nums',
          fontSize: '0.78rem',
        }}
      />
    </label>
  );
}

const liStyle = { color: 'var(--muted)', fontSize: '0.82rem' };
