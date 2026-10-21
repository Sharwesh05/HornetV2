// Small inline SVG icon set (no icon library). Icons are decorative: the text next to them carries the meaning,
// so they are hidden from screen readers.
import type { ReactNode } from "react";

function Svg({ children, size = 18, className }: { children: ReactNode; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" className={className}>
      {children}
    </svg>
  );
}

type P = { size?: number; className?: string };

export const HexLogo = ({ size = 28, className }: P) => (
  <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className={className}>
    <path d="M16 2.5 28 9.25v13.5L16 29.5 4 22.75V9.25Z" fill="var(--honey-fill)" />
    <path d="M16 9.5 22 12.9v6.2L16 22.5 10 19.1v-6.2Z" fill="var(--on-honey)" opacity="0.9" />
    <path d="M16 13.2 18.8 14.8v2.4L16 18.8l-2.8-1.6v-2.4Z" fill="var(--honey-fill)" />
  </svg>
);
export const IconHome = (p: P) => <Svg {...p}><path d="M3 11.5 12 4l9 7.5" /><path d="M5.5 10v9.5h13V10" /></Svg>;
export const IconSessions = (p: P) => <Svg {...p}><circle cx="9" cy="8" r="3.2" /><path d="M3.5 19c.8-3.2 3-5 5.5-5s4.7 1.8 5.5 5" /><path d="M16 4.8a3.2 3.2 0 0 1 0 6.4M17.5 14c1.6.6 2.7 2.4 3 5" /></Svg>;
export const IconEnvs = (p: P) => <Svg {...p}><rect x="3.5" y="4" width="17" height="6" rx="1.5" /><rect x="3.5" y="14" width="17" height="6" rx="1.5" /><path d="M7 7h.01M7 17h.01" /></Svg>;
export const IconHelp = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="8.5" /><path d="M9.6 9.5a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.7-.9 1.3v.6" /><path d="M12 16.8h.01" /></Svg>;
export const IconSun = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" /></Svg>;
export const IconMoon = (p: P) => <Svg {...p}><path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z" /></Svg>;
export const IconMonitor = (p: P) => <Svg {...p}><rect x="3" y="4.5" width="18" height="12" rx="1.5" /><path d="M9 20h6M12 16.5V20" /></Svg>;
export const IconLock = (p: P) => <Svg {...p}><rect x="5" y="10.5" width="14" height="9.5" rx="1.5" /><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" /></Svg>;
export const IconPlay = (p: P) => <Svg {...p}><path d="M7 5.5v13l11-6.5Z" fill="currentColor" stroke="none" /></Svg>;
export const IconPause = (p: P) => <Svg {...p}><path d="M8 5.5v13M16 5.5v13" strokeWidth={3} /></Svg>;
export const IconPrev = (p: P) => <Svg {...p}><path d="M15 6l-6 6 6 6" /></Svg>;
export const IconNext = (p: P) => <Svg {...p}><path d="M9 6l6 6-6 6" /></Svg>;
export const IconFirst = (p: P) => <Svg {...p}><path d="M17 6l-6 6 6 6M7 6v12" /></Svg>;
export const IconLast = (p: P) => <Svg {...p}><path d="M7 6l6 6-6 6M17 6v12" /></Svg>;
export const IconCheck = (p: P) => <Svg {...p}><path d="m5 12.5 4.5 4.5L19 7.5" /></Svg>;
export const IconSpark = (p: P) => <Svg {...p}><path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" /></Svg>;
export const IconFile = (p: P) => <Svg {...p}><path d="M6.5 3.5h7l4 4v13h-11Z" /><path d="M13.5 3.5v4h4" /></Svg>;
export const IconFolder = (p: P) => <Svg {...p}><path d="M3.5 6.5h6l2 2h9v10h-17Z" /></Svg>;
export const IconKey = (p: P) => <Svg {...p}><circle cx="8" cy="15" r="4" /><path d="m11 12 8-8M16 7l2.5 2.5" /></Svg>;
export const IconDatabase = (p: P) => <Svg {...p}><ellipse cx="12" cy="6" rx="7" ry="2.8" /><path d="M5 6v12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8V6M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8" /></Svg>;
export const IconGlobe = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c2.3 2.4 3.4 5.2 3.4 8.5s-1.1 6.1-3.4 8.5c-2.3-2.4-3.4-5.2-3.4-8.5S9.7 5.9 12 3.5Z" /></Svg>;
export const IconTerminal = (p: P) => <Svg {...p}><rect x="3" y="4.5" width="18" height="15" rx="1.5" /><path d="m7 9 3 3-3 3M12.5 15H17" /></Svg>;
export const IconShield = (p: P) => <Svg {...p}><path d="M12 3.5 19 6v5.5c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6Z" /></Svg>;
export const IconAlert = (p: P) => <Svg {...p}><path d="M12 4 21 19.5H3Z" /><path d="M12 10v4.5M12 17.2h.01" /></Svg>;
export const IconLogin = (p: P) => <Svg {...p}><path d="M14 4.5h4.5v15H14" /><path d="M4 12h10M10.5 8.5 14 12l-3.5 3.5" /></Svg>;
export const IconPlus = (p: P) => <Svg {...p}><path d="M12 5v14M5 12h14" /></Svg>;
export const IconBox = (p: P) => <Svg {...p}><path d="m12 3.5 8 4.5v8l-8 4.5-8-4.5V8Z" /><path d="m4 8 8 4.5L20 8M12 12.5v8" /></Svg>;
