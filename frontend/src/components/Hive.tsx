import type { Session } from "../lib/types";
import { plainStatus } from "../lib/story";
import { shortId, time } from "../lib/text";

const R = 22; // hexagon radius
const W = Math.sqrt(3) * R;

function hexPoints(cx: number, cy: number): string {
  return Array.from({ length: 6 }, (_, i) => {
    const a = (Math.PI / 180) * (60 * i - 30);
    return `${(cx + R * Math.cos(a)).toFixed(1)},${(cy + R * Math.sin(a)).toFixed(1)}`;
  }).join(" ");
}

/**
 * The hive: one hexagon per recent visit, newest first, laid out like honeycomb. Filled honey = live now, outlined =
 * ended, red = stopped by a safety limit. Each cell is a link with a full text label (colour is not the only cue).
 */
export function Hive({ sessions }: { sessions: Session[] }) {
  const perRow = 12;
  const cells = sessions.slice(0, 36);
  const rows = Math.max(1, Math.ceil(cells.length / perRow));
  const width = perRow * W + W / 2 + 4;
  const height = rows * R * 1.5 + R / 2 + 4;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full max-w-3xl" role="list" aria-label="Recent visits">
      {cells.map((s, i) => {
        const row = Math.floor(i / perRow);
        const col = i % perRow;
        const cx = col * W + W / 2 + (row % 2 ? W / 2 : 0) + 2;
        const cy = row * R * 1.5 + R + 2;
        const st = plainStatus(s);
        const live = st.tone === "attention";
        const fill = live ? "var(--honey-fill)" : st.tone === "danger" ? "var(--danger-soft)" : "var(--surface)";
        const stroke = live ? "var(--honey-fill)" : st.tone === "danger" ? "var(--danger)" : "var(--line)";
        const label = `Visit ${shortId(s.id)}, ${st.text}, started ${time(s.started_at)}${s.commands !== undefined ? `, ${s.commands} commands` : ""}`;
        return (
          <a key={s.id} href={`#/sessions/${s.id}`} role="listitem" aria-label={label}>
            <title>{label}</title>
            <polygon points={hexPoints(cx, cy)} fill={fill} stroke={stroke} strokeWidth={2}
              className={`transition-[filter] hover:brightness-90 ${live ? "hive-live" : ""}`} />
            <text x={cx} y={cy + 4} textAnchor="middle" fontSize="11" fontWeight="700"
              fill={live ? "var(--on-honey)" : "var(--muted)"} aria-hidden="true">
              {s.commands ?? ""}
            </text>
          </a>
        );
      })}
    </svg>
  );
}
