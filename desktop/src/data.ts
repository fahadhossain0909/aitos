import type { Readiness } from "./ReadinessPanel";
export type Position = {
  trade_id: string;
  symbol: string;
  side: string;
  quantity: number;
  entry_price: number;
  mark_price?: number;
  position_size_usd: number;
  risk_amount_usd: number;
  pnl: number | null;
  state: string;
  entry_time: string;
  exit_time?: string;
  exit_price?: number;
  sl_price?: number;
  tp_price?: number;
};
export type Stage = {
  stage: string;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  latency_p99_ms: number | null;
  latency_sample_count: number;
  input_count: number;
  queue_depth: number;
  error_count: number;
};
export type Snapshot = {
  readiness?: Readiness;
  projection?: import("./projectionClient").ProjectionReport;
  connected: boolean;
  source: string;
  mode: string;
  generated_at: string;
  controls: string;
  positions: Position[];
  pipeline: Stage[];
  modules: { name: string; status: string }[];
  configuration: Record<string, unknown>;
  history_available: boolean;
};
export const samplePositions: Position[] = [
  {
    trade_id: "sample-btc",
    symbol: "BTCUSDT",
    side: "LONG",
    quantity: 0.12,
    entry_price: 67420,
    mark_price: 68182,
    position_size_usd: 8181.84,
    risk_amount_usd: 84,
    pnl: 91.44,
    state: "position_opened",
    entry_time: "2026-11-07T08:42:00Z",
    sl_price: 66720,
    tp_price: 69400,
  },
  {
    trade_id: "sample-eth",
    symbol: "ETHUSDT",
    side: "LONG",
    quantity: 2.4,
    entry_price: 3484,
    mark_price: 3512,
    position_size_usd: 8428.8,
    risk_amount_usd: 72,
    pnl: 67.2,
    state: "position_opened",
    entry_time: "2026-11-07T09:16:00Z",
    sl_price: 3454,
    tp_price: 3600,
  },
  {
    trade_id: "sample-sol",
    symbol: "SOLUSDT",
    side: "SHORT",
    quantity: 18,
    entry_price: 174.8,
    mark_price: 172.45,
    position_size_usd: 3104.1,
    risk_amount_usd: 63,
    pnl: 42.3,
    state: "position_opened",
    entry_time: "2026-11-07T09:32:00Z",
    sl_price: 178.3,
    tp_price: 168.5,
  },
];
export const sampleHistory: Position[] = [312, -87, 189, 415, -63, 76].map(
  (pnl, i) => ({
    ...samplePositions[i % 3],
    trade_id: `sample-history-${i}`,
    pnl,
    state: "position_closed",
    entry_time: `2026-11-0${7 - i}T08:12:00Z`,
    exit_time: `2026-11-0${7 - i}T10:32:00Z`,
    exit_price: samplePositions[i % 3].entry_price + (i % 2 ? -12 : 32),
  }),
);
export const sampleStages: Stage[] = [
  "Transport",
  "Decode",
  "Normalize",
  "Redis publish",
  "Redis consumer",
  "Live state",
  "Features",
  "Scanner",
  "Trade lifecycle",
  "Persistence",
].map((stage, i) => ({
  stage,
  latency_p50_ms: [1.2, 0.4, 0.7, 2.3, 3.2, 1.5, 7.8, 12.4, 18.2, 9.4][i],
  latency_p95_ms: [2.4, 0.9, 1.3, 5.2, 6.8, 3.4, 14.3, 24.8, 37.4, 18.9][i],
  latency_p99_ms: [4.1, 1.5, 2.1, 8.1, 9.4, 5.7, 19.8, 31.4, 48.2, 26.1][i],
  latency_sample_count: 240,
  input_count: 182400 - i * 31,
  queue_depth: i === 4 ? 2 : 0,
  error_count: 0,
}));
export const markets = [
  {
    symbol: "BTC",
    name: "Bitcoin",
    price: 68182,
    change: 1.84,
    color: "#edb76c",
  },
  {
    symbol: "ETH",
    name: "Ethereum",
    price: 3512,
    change: 1.23,
    color: "#b1a7d5",
  },
  {
    symbol: "SOL",
    name: "Solana",
    price: 172.45,
    change: -0.72,
    color: "#a5d7b4",
  },
  { symbol: "BNB", name: "BNB", price: 589.2, change: 2.16, color: "#d9ce83" },
  {
    symbol: "AVAX",
    name: "Avalanche",
    price: 34.92,
    change: -1.21,
    color: "#e9a497",
  },
];
export const money = (n: number | null | undefined, digits = 2) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      }).format(n);
export const number = (n: number | null | undefined, digits = 1) =>
  n == null || !Number.isFinite(n)
    ? "—"
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(
        n,
      );
export function exportCsv(rows: Record<string, unknown>[], filename: string) {
  const keys = Object.keys(rows[0] || {});
  const cell = (x: unknown) =>
    `"${String(typeof x === "string" && /^\s*[=+@-]/.test(x) ? `'${x}` : (x ?? "")).replaceAll('"', '""')}"`;
  const blob = new Blob(
    [
      [keys, ...rows.map((r) => keys.map((k) => r[k]))]
        .map((r) => r.map(cell).join(","))
        .join("\n"),
    ],
    { type: "text/csv" },
  );
  download(blob, filename);
}
export function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export const checkDraft = (d: {
  topN: number;
  interval: number;
  risk: number;
  leverage: number;
}) =>
  Number.isInteger(d.topN) &&
  d.topN >= 1 &&
  d.topN <= 1000 &&
  Number.isFinite(d.interval) &&
  d.interval > 0 &&
  d.interval <= 3600 &&
  Number.isFinite(d.risk) &&
  d.risk > 0 &&
  d.risk <= 1 &&
  Number.isFinite(d.leverage) &&
  d.leverage >= 1 &&
  d.leverage <= 10;
