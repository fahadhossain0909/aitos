import { useState } from "react";
import { ArrowUpRight, CircleHelp } from "lucide-react";
import { money, number } from "../data";

import { Icon, Empty } from "./common";

export function EquityChart({
  demo,
  range,
  onRange,
}: {
  demo: boolean;
  range: string;
  onRange: (s: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const start = (
    {
      "1D": 127608.25,
      "1W": 124820,
      "1M": 119240,
      "3M": 110310,
      "1Y": 100000,
    } as Record<string, number>
  )[range];
  const end = 128450.25;
  const values = Array.from(
    { length: 85 },
    (_, i) =>
      start +
      (end - start) * (i / 84) +
      Math.sin(i * 0.34) *
        (end - start) *
        0.045 *
        Math.sin((i / 84) * Math.PI) +
      Math.sin(i * 0.94) * (end - start) * 0.018 * Math.sin((i / 84) * Math.PI),
  );
  const width = 880,
    height = 224;
  const pts = values.map((v, i) => [
    (i / 84) * width,
    201 - ((v - start) / (end - start)) * 172,
  ]);
  const line = pts
    .map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const ix = hover ?? 84;
  return (
    <section className="panel equity-panel">
      <div className="panel-heading">
        <div className="panel-title">
          <span className="small-cap">PORTFOLIO VALUE</span>
          <span className="tiny-tag">
            {demo ? "SAMPLE SERIES" : "NO EQUITY FEED"}
          </span>
        </div>
        <div className="segmented">
          {["1D", "1W", "1M", "3M", "1Y"].map((r) => (
            <button
              key={r}
              aria-pressed={range === r}
              className={range === r ? "active" : ""}
              onClick={() => {
                onRange(r);
                setHover(null);
              }}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
      <div className="equity-number">
        {demo ? money(values[ix]) : "—"}
        <span className="unit">USD</span>
      </div>
      <div className="equity-context">
        {demo ? (
          <>
            <span className="positive">
              <ArrowUpRight size={14} /> {money(end - start)} (
              {((end / start - 1) * 100).toFixed(2)}%)
            </span>
            <span>this period · illustrative</span>
          </>
        ) : (
          <span>Historical account-equity feed has not been attached.</span>
        )}
      </div>
      <div className="chart-container">
        {demo ? (
          <>
            <div className="chart-y-labels">
              {[130000, 125000, 120000, 115000].map((v, i) => (
                <span key={v}>
                  {number(start + (end - start) * (1.12 - i * 0.35), 0)}
                </span>
              ))}
            </div>
            <svg
              className="equity-chart"
              viewBox={`-2 0 ${width + 4} ${height}`}
              preserveAspectRatio="none"
              onMouseMove={(e) => {
                const b = e.currentTarget.getBoundingClientRect();
                setHover(
                  Math.max(
                    0,
                    Math.min(
                      84,
                      Math.round(((e.clientX - b.left) / b.width) * 84),
                    ),
                  ),
                );
              }}
              onMouseLeave={() => setHover(null)}
              role="img"
              aria-label={`Illustrative portfolio value over ${range}`}
            >
              <defs>
                <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--c-c5ee9c)" stopOpacity=".17" />
                  <stop offset="100%" stopColor="var(--c-c5ee9c)" stopOpacity="0" />
                </linearGradient>
              </defs>
              {[28, 83, 138, 193].map((y) => (
                <line
                  key={y}
                  x1="0"
                  x2={width}
                  y1={y}
                  y2={y}
                  stroke="var(--c-30342c)"
                  strokeDasharray="3 5"
                />
              ))}
              <path d={`${line} L${width},224 L0,224 Z`} fill="url(#area)" />
              <path
                d={line}
                fill="none"
                stroke="var(--c-caeea0)"
                strokeWidth="2.2"
                vectorEffect="non-scaling-stroke"
              />
              {hover != null && (
                <line
                  x1={pts[ix][0]}
                  x2={pts[ix][0]}
                  y1="10"
                  y2="224"
                  stroke="var(--c-929986)"
                  strokeDasharray="3 4"
                />
              )}
              <circle
                cx={pts[ix][0]}
                cy={pts[ix][1]}
                r="4"
                fill="var(--c-dbf9bc)"
                stroke="var(--c-22291c)"
                strokeWidth="5"
                vectorEffect="non-scaling-stroke"
              />
            </svg>
            <div className="chart-x-labels">
              {(range === "1D"
                ? ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]
                : [
                    "08 OCT",
                    "13 OCT",
                    "18 OCT",
                    "23 OCT",
                    "28 OCT",
                    "02 NOV",
                    "07 NOV",
                  ]
              ).map((v) => (
                <span key={v}>{v}</span>
              ))}
            </div>
          </>
        ) : (
          <Empty
            title="Equity history not measured"
            description="The console will not substitute an illustrative curve for a connected account. Trade history is available separately when ClickHouse is attached."
          />
        )}
      </div>
      <div className="chart-footer">
        <div>
          <span className="line-key" /> Portfolio value{" "}
          <span className="dim">
            · {demo ? "Sample workspace" : "Unavailable"}
          </span>
        </div>
        <span>
          Denominated in USD <Icon as={CircleHelp} size={12} />
        </span>
      </div>
    </section>
  );
}
