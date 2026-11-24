import { ArrowDownLeft, ArrowUpRight, ChevronRight } from "lucide-react";
import { money, number } from "../data";
import type { Position } from "../data";

import { Empty, Coin } from "./common";

export function PositionTable({
  rows,
  compact = false,
  onSelect,
}: {
  rows: Position[];
  compact?: boolean;
  onSelect: (p: Position) => void;
}) {
  return rows.length ? (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Instrument</th>
            <th>Direction</th>
            <th>Entry price</th>
            {!compact && <th>Quantity</th>}
            <th>Position value</th>
            <th>Recorded P&L</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr
              key={p.trade_id}
              onClick={() => onSelect(p)}
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && onSelect(p)}
              aria-label={`Inspect ${p.symbol} position`}
            >
              <td>
                <div className="instrument">
                  <Coin symbol={p.symbol} />
                  <div>
                    <strong>
                      {p.symbol.replace("USDT", "")}
                      <span className="dim"> / USDT</span>
                    </strong>
                    <small>Perpetual futures</small>
                  </div>
                </div>
              </td>
              <td>
                <span
                  className={`direction ${p.side === "LONG" ? "positive" : "negative"}`}
                >
                  {p.side === "LONG" ? (
                    <ArrowUpRight size={13} />
                  ) : (
                    <ArrowDownLeft size={13} />
                  )}{" "}
                  {p.side}
                </span>
              </td>
              <td className="mono">{money(p.entry_price)}</td>
              {!compact && <td className="mono">{number(p.quantity, 5)}</td>}
              <td className="mono">{money(p.position_size_usd)}</td>
              <td
                className={`mono ${p.pnl != null && p.pnl < 0 ? "negative" : "positive"}`}
              >
                {p.pnl != null && p.pnl > 0 ? "+" : ""}
                {money(p.pnl)}
              </td>
              <td>
                <ChevronRight size={15} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty
      title="No positions to display"
      description="Attach a running backend to read actual open positions. No positions are inferred from a disconnected runtime."
    />
  );
}
