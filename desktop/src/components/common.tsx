import { Database } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { number } from "../data";

export function Icon({
  as: Component,
  ...props
}: {
  as: LucideIcon;
  size?: number;
  className?: string;
}) {
  return <Component size={16} strokeWidth={1.7} {...props} />;
}

export function Pill({
  children,
  tone = "muted",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

export function Empty({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty">
      <Database size={27} />
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}

export function Sparkline({
  positive = true,
  seed = 0,
}: {
  positive?: boolean;
  seed?: number;
}) {
  const path = Array.from(
    { length: 24 },
    (_, i) =>
      `${i ? "L" : "M"}${i * 3.5},${20 - (Math.sin(i * 0.7 + seed) * 4 + i * 0.45) * (positive ? 1 : -0.6)}`,
  ).join(" ");
  return (
    <svg
      viewBox="0 0 84 34"
      className={`sparkline ${positive ? "positive" : "negative"}`}
      aria-hidden="true"
    >
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export function Coin({ symbol }: { symbol: string }) {
  const base = symbol.replace("USDT", "");
  return (
    <span className={`coin coin-${base.toLowerCase()}`}>
      {base === "BTC"
        ? "₿"
        : base === "ETH"
          ? "Ξ"
          : base === "SOL"
            ? "≋"
            : base[0]}
    </span>
  );
}
