import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = fileURLToPath(new URL(".", import.meta.url));
const files = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? files(join(dir, e.name)) : [join(dir, e.name)],
  );
const sources = files(root).filter(
  (f) => /\.(css|tsx)$/.test(f) && !/(tokens|themes|signer)\.css$/.test(f), // signer.css belongs to the separate trusted-signer window, which does not load the theme layer
);
const HEX = /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])/;
const tokens = readFileSync(join(root, "tokens.css"), "utf8");
const blocks = tokens.split(/\n}\n/).filter((b) => b.includes("--c-"));

describe("theme design tokens", () => {
  it("defines three theme palettes", () => expect(blocks).toHaveLength(3));
  it("keeps raw colour literals out of component styles", () => {
    const offenders = sources.filter((f) => HEX.test(readFileSync(f, "utf8")));
    expect(offenders.map((f) => f.replace(root, ""))).toEqual([]);
  });
  it("defines every token used by the UI in every theme", () => {
    const used = new Set<string>();
    for (const f of sources)
      for (const m of readFileSync(f, "utf8").matchAll(/var\((--c-[0-9a-f]+)\)/g)) used.add(m[1]);
    expect(used.size).toBeGreaterThan(100);
    for (const block of blocks)
      for (const name of used) expect(block, name).toContain(`${name}:`);
  });
});
