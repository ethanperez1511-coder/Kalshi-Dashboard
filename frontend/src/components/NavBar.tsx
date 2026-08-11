import { NavLink } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import type { SystemStatus } from '../types/api';

const links = [
  { to: '/', label: 'Overview' },
  { to: '/markets', label: 'Markets' },
  { to: '/backtest', label: 'Backtest' },
  { to: '/review', label: 'Review' },
  { to: '/settings', label: 'Settings' },
];

export default function NavBar() {
  const { data: status } = useApi<SystemStatus>('/api/status', 30000);
  const online = status?.mode === 'online';

  return (
    <nav style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '0 var(--gap-lg)', height: 52,
      background: 'rgba(10, 11, 14, 0.82)',
      borderBottom: '1px solid var(--hairline)',
      position: 'sticky', top: 0, zIndex: 100,
      backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)',
    }}>
      {/* Left: Logo + Nav links */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--gap-lg)' }}>
        {/* Logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{
            width: 22, height: 22, borderRadius: 6,
            background: 'linear-gradient(135deg, #45d6bf, #2fb8a3)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 11, fontWeight: 700, color: '#0a0b0e',
            fontFamily: 'var(--font-mono)',
          }}>K</div>
          <span style={{
            fontFamily: 'var(--font-mono)', fontWeight: 600, fontSize: '0.92rem',
            color: 'var(--text)', letterSpacing: '-0.3px',
          }}>
            KALSHI<span style={{ color: 'var(--accent)', marginLeft: 3 }}>DASH</span>
          </span>
        </div>

        {/* Nav pills */}
        <div style={{ display: 'flex', gap: 2 }}>
          {links.map(l => (
            <NavLink key={l.to} to={l.to} end={l.to === '/'} style={({ isActive }) => ({
              padding: '6px 14px', borderRadius: 'var(--radius-sm)',
              fontSize: '0.82rem', fontWeight: 500,
              color: isActive ? 'var(--accent)' : 'var(--muted)',
              background: isActive ? 'var(--accent-dim)' : 'transparent',
              transition: 'color 0.15s, background 0.15s',
            })}>
              {l.label}
            </NavLink>
          ))}
        </div>
      </div>

      {/* Right: Status + Paper badge */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--gap-md)' }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6,
          fontSize: '0.75rem', color: 'var(--muted)',
        }}>
          <div style={{
            width: 7, height: 7, borderRadius: '50%',
            background: online ? 'var(--positive)' : 'var(--faint)',
            animation: online ? 'pulse 2s ease-in-out infinite' : 'none',
          }} />
          {online ? 'Online' : 'Offline'}
        </div>

        <div style={{
          padding: '3px 10px', borderRadius: 'var(--radius-sm)',
          fontSize: '0.7rem', fontFamily: 'var(--font-mono)', fontWeight: 600,
          letterSpacing: '0.08em', textTransform: 'uppercase',
          color: 'var(--warning)',
          border: '1px solid var(--warning-border)',
          background: 'transparent',
        }}>
          PAPER
        </div>
      </div>
    </nav>
  );
}
