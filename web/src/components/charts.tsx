// Hand-rolled inline-SVG charts — deliberately dependency-free.
// Fixed internal viewBox, responsive via width:100%.

export interface ChartPoint {
  x: number; // usually a unix-ms timestamp
  y: number;
}

const W = 600;
const H = 180;
const PAD = { top: 12, right: 12, bottom: 22, left: 44 };

function niceRange(min: number, max: number): [number, number] {
  if (min === max) {
    const pad = Math.max(1, Math.abs(min) * 0.1);
    return [min - pad, max + pad];
  }
  const pad = (max - min) * 0.08;
  return [min - pad, max + pad];
}

const fmtNum = (n: number) =>
  Math.abs(n) >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(Math.round(n));

/**
 * Minimal line chart: min/max scaling, a few horizontal gridlines, start/end
 * x-axis labels. One point renders as a dot plus a "not enough history" note.
 */
export function LineChart({
  points,
  color = "var(--color-brand-bright)",
  yFmt = fmtNum,
  xFmt = (x: number) => new Date(x).toLocaleDateString(),
  yMin,
  className,
}: {
  points: ChartPoint[];
  color?: string;
  yFmt?: (y: number) => string;
  xFmt?: (x: number) => string;
  /**
   * Hard floor for the y axis. Counts pass 0 so the symmetric padding below the
   * minimum cannot label the axis with an impossible negative value.
   */
  yMin?: number;
  className?: string;
}) {
  if (points.length === 0) {
    return (
      <div className="grid h-40 place-items-center rounded-lg border border-dashed border-border text-xs text-faint">
        No data yet
      </div>
    );
  }

  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const raw = niceRange(Math.min(...ys), Math.max(...ys));
  const yLo = yMin === undefined ? raw[0] : Math.max(yMin, raw[0]);
  const yHi = yLo === raw[1] ? raw[1] + 1 : raw[1];
  const xLo = Math.min(...xs);
  const xHi = Math.max(...xs);
  const xSpan = xHi - xLo || 1;

  const px = (x: number) => PAD.left + ((x - xLo) / xSpan) * (W - PAD.left - PAD.right);
  const py = (y: number) => PAD.top + (1 - (y - yLo) / (yHi - yLo)) * (H - PAD.top - PAD.bottom);

  const single = points.length === 1;
  const path = points
    .slice()
    .sort((a, b) => a.x - b.x)
    .map((p, i) => `${i === 0 ? "M" : "L"}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`)
    .join(" ");
  const last = points[points.length - 1];

  const gridYs = [0.25, 0.5, 0.75].map((t) => PAD.top + t * (H - PAD.top - PAD.bottom));

  return (
    <div className={className}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img">
        {gridYs.map((gy) => (
          <line
            key={gy}
            x1={PAD.left}
            x2={W - PAD.right}
            y1={gy}
            y2={gy}
            stroke="var(--color-border)"
            strokeWidth="1"
            strokeDasharray="3 5"
          />
        ))}
        {/* y-axis min/max labels */}
        <text x={PAD.left - 6} y={PAD.top + 4} textAnchor="end" className="fill-[var(--color-faint)] text-[10px]">
          {yFmt(yHi)}
        </text>
        <text
          x={PAD.left - 6}
          y={H - PAD.bottom}
          textAnchor="end"
          className="fill-[var(--color-faint)] text-[10px]"
        >
          {yFmt(yLo)}
        </text>
        {/* x-axis start/end labels */}
        <text x={PAD.left} y={H - 6} textAnchor="start" className="fill-[var(--color-faint)] text-[10px]">
          {xFmt(xLo)}
        </text>
        <text x={W - PAD.right} y={H - 6} textAnchor="end" className="fill-[var(--color-faint)] text-[10px]">
          {xFmt(xHi)}
        </text>

        {!single && (
          <path d={path} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
        )}
        <circle cx={px(last.x)} cy={py(last.y)} r={single ? 4 : 3} fill={color} />
      </svg>
      {single && (
        <p className="mt-1 text-center text-[0.7rem] text-faint">
          Not enough history yet — the line appears as snapshots accumulate.
        </p>
      )}
    </div>
  );
}

/** Minimal bar chart with start/end x labels and a max-value y label. */
export function BarChart({
  bars,
  color = "var(--color-brand)",
  yFmt = fmtNum,
  className,
}: {
  bars: { label: string; value: number }[];
  color?: string;
  yFmt?: (y: number) => string;
  className?: string;
}) {
  if (bars.length === 0) {
    return (
      <div className="grid h-40 place-items-center rounded-lg border border-dashed border-border text-xs text-faint">
        No data yet
      </div>
    );
  }
  const max = Math.max(1, ...bars.map((b) => b.value));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const step = innerW / bars.length;
  const barW = Math.max(2, Math.min(22, step * 0.7));

  return (
    <div className={className}>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img">
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={H - PAD.bottom}
          y2={H - PAD.bottom}
          stroke="var(--color-border)"
          strokeWidth="1"
        />
        <text x={PAD.left - 6} y={PAD.top + 4} textAnchor="end" className="fill-[var(--color-faint)] text-[10px]">
          {yFmt(max)}
        </text>
        <text x={PAD.left - 6} y={H - PAD.bottom} textAnchor="end" className="fill-[var(--color-faint)] text-[10px]">
          0
        </text>
        {bars.map((b, i) => {
          const h = (b.value / max) * innerH;
          const x = PAD.left + i * step + (step - barW) / 2;
          return (
            <rect
              key={`${b.label}-${i}`}
              x={x}
              y={H - PAD.bottom - h}
              width={barW}
              height={Math.max(b.value > 0 ? 2 : 0, h)}
              rx="2"
              fill={color}
              opacity={0.9}
            >
              <title>{`${b.label}: ${b.value}`}</title>
            </rect>
          );
        })}
        <text x={PAD.left} y={H - 6} textAnchor="start" className="fill-[var(--color-faint)] text-[10px]">
          {bars[0].label}
        </text>
        <text x={W - PAD.right} y={H - 6} textAnchor="end" className="fill-[var(--color-faint)] text-[10px]">
          {bars[bars.length - 1].label}
        </text>
      </svg>
    </div>
  );
}

/** Horizontal stacked distribution bar with legend + counts. */
export function StackedBar({
  segments,
  className,
}: {
  segments: { label: string; value: number; color: string }[];
  className?: string;
}) {
  const total = segments.reduce((n, s) => n + s.value, 0);
  return (
    <div className={className}>
      <div className="flex h-3.5 w-full overflow-hidden rounded-full bg-bg-elevated">
        {total > 0 &&
          segments
            .filter((s) => s.value > 0)
            .map((s) => (
              <div
                key={s.label}
                style={{ width: `${(s.value / total) * 100}%`, backgroundColor: s.color }}
                title={`${s.label}: ${s.value}`}
              />
            ))}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5">
        {segments.map((s) => (
          <span key={s.label} className="inline-flex items-center gap-1.5 text-xs text-muted">
            <span className="size-2.5 rounded-sm" style={{ backgroundColor: s.color }} />
            {s.label}
            <span className="font-semibold tabular-nums text-fg">{s.value}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
