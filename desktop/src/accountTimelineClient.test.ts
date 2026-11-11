import { describe, it, expect } from "vitest";
import { parseTimelinePage } from "./accountTimelineClient";
const valid = () => ({schema_version:1,source:"durable_account_observations",consistent_insert_cut:true,upper_id:3,rows:[{id:3,observed_at:"2026-11-06T12:00:00Z",totals:{totalWalletBalance:"10.000000000000000001",totalMarginBalance:"11",totalUnrealizedProfit:"1",totalInitialMargin:"2",totalMaintMargin:"1",availableBalance:"8"}}],next_cursor:"signed_cursor",limitations:[]});
describe("timeline",()=>{
 it("preserves historical values without current freshness claims",()=>expect(parseTimelinePage(valid()).rows[0].totals.totalWalletBalance).toBe("10.000000000000000001"));
 it("rejects rows outside or repeated within pinned cut",()=>{const s=valid();s.rows[0].id=4;expect(()=>parseTimelinePage(s)).toThrow();const t=valid();t.rows.push(t.rows[0]);expect(()=>parseTimelinePage(t)).toThrow();});
 it("rejects arbitrary cursor URLs and malformed money",()=>{expect(()=>parseTimelinePage({...valid(),next_cursor:"https://evil"})).toThrow();const s=valid();s.rows[0].totals.totalWalletBalance="NaN";expect(()=>parseTimelinePage(s)).toThrow();});
 it("rejects empty continuation and mutable offset claim",()=>{expect(()=>parseTimelinePage({...valid(),rows:[]})).toThrow();expect(()=>parseTimelinePage({...valid(),consistent_insert_cut:false})).toThrow();});
});
