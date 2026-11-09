import { describe, it, expect } from "vitest";
import { parseAccountObservation } from "./accountClient";
const now = Date.parse("2026-11-06T12:00:00Z");
const valid = () => ({ schema_version: 1, source: "binance_signed_rest", controls: "read_only", consistent_snapshot: false, observed_at: new Date(now).toISOString(), totals: {totalWalletBalance:"100.000000000000000001", totalUnrealizedProfit:"-1",totalMarginBalance:"99.000000000000000001",totalInitialMargin:"10",totalMaintMargin:"1",availableBalance:"89"}, positions: [{symbol:"BTCUSDT",side:"LONG",quantity:"0.01",entryPrice:"10000",markPrice:"9900",unRealizedProfit:"-1"}], limitations:["cross_endpoint_cut_not_atomic"] });
describe("account observation", () => {
 it("preserves exact decimal strings", () => expect(parseAccountObservation(valid(),now).totals.totalWalletBalance).toBe("100.000000000000000001"));
 for(const [key,value] of [["observed_at","2020-01-01T00:00:00Z"],["controls","write"],["consistent_snapshot",true],["source","sample"]] as const)
  it(`rejects ${key} mismatch`,()=>expect(()=>parseAccountObservation({...valid(),[key]:value},now)).toThrow());
 it("rejects missing totals and numeric coercion",()=>{
  const s=valid(); (s.totals as Record<string,unknown>).availableBalance=0;
  expect(()=>parseAccountObservation(s,now)).toThrow();
 });
 it("rejects duplicate positions",()=>{const s=valid();s.positions.push(s.positions[0]);expect(()=>parseAccountObservation(s,now)).toThrow();});
 it("rejects malformed decimal or unbounded positions",()=>{const s=valid();s.positions[0].markPrice="NaN";expect(()=>parseAccountObservation(s,now)).toThrow();expect(()=>parseAccountObservation({...valid(),positions:Array(2001).fill(valid().positions[0])},now)).toThrow();});
});
