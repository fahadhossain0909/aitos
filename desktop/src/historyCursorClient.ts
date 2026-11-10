import { parseHistory } from "./consoleClient";
import type { Position } from "./data";
export type HistoryCut = {
  rows: Position[];
  next_cursor: string | null;
  page_cursor: string;
  snapshot_id: string;
  total_captured: number;
  truncated: boolean;
};
export function parseHistoryCut(input: unknown, now = Date.now()): HistoryCut {
  const s = input as Record<string, unknown> | null;
  if (
    !s ||
    s.schema_version !== 1 ||
    s.source !== "materialized_ledger_query" ||
    s.consistent_pages !== true ||
    s.available !== true ||
    typeof s.snapshot_id !== "string" ||
    !/^[a-f0-9]{32}$/.test(s.snapshot_id) ||
    typeof s.page_cursor !== "string" ||
    !/^[A-Za-z0-9_-]{1,256}$/.test(s.page_cursor) ||
    typeof s.truncated !== "boolean" ||
    !Number.isSafeInteger(s.total_captured) ||
    (s.total_captured as number) < 0 ||
    (s.total_captured as number) > 10000 ||
    typeof s.captured_at !== "number" ||
    typeof s.expires_at !== "number" ||
    s.expires_at - s.captured_at !== 900 ||
    now / 1000 < s.captured_at - 5 ||
    now / 1000 > s.expires_at ||
    (s.next_cursor !== null &&
      (typeof s.next_cursor !== "string" ||
        !/^[A-Za-z0-9_-]{1,256}$/.test(s.next_cursor)))
  )
    throw Error("Invalid or expired materialized history cut");
  const rows = parseHistory({ ...s, source: "clickhouse" });
  if (
    rows.length > (s.total_captured as number) ||
    (!rows.length && s.next_cursor !== null)
  )
    throw Error("Invalid history continuation");
  return {
    rows,
    page_cursor: s.page_cursor,
    next_cursor: s.next_cursor as string | null,
    snapshot_id: s.snapshot_id,
    total_captured: s.total_captured as number,
    truncated: s.truncated,
  };
}
