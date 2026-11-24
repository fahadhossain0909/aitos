// Appearance preferences: colour theme + text size. Pure helpers are unit-tested;
// DOM/storage access is isolated in apply/load/save and always fails safe.
export const THEMES = ["olive", "terminal", "ivory", "system"] as const;
export type ThemePref = (typeof THEMES)[number];
export type ResolvedTheme = Exclude<ThemePref, "system">;
export const TEXT_SIZES = ["100", "115", "130"] as const;
export type TextSize = (typeof TEXT_SIZES)[number];

const THEME_KEY = "aitos-theme-v1";
const TEXT_KEY = "aitos-text-v1";
export const THEME_COLOR: Record<ResolvedTheme, string> = {
  olive: "#111310",
  terminal: "#0d0f12",
  ivory: "#f3f0e6",
};

/** Unknown/corrupt values fall back to the original olive-dark look. */
export function parseTheme(value: unknown): ThemePref {
  return THEMES.includes(value as ThemePref) ? (value as ThemePref) : "olive";
}
export function parseTextSize(value: unknown): TextSize {
  return TEXT_SIZES.includes(value as TextSize) ? (value as TextSize) : "100";
}
export function resolveTheme(pref: ThemePref, prefersLight: boolean): ResolvedTheme {
  if (pref !== "system") return pref;
  return prefersLight ? "ivory" : "olive";
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
export function loadAppearance(): { theme: ThemePref; text: TextSize } {
  return { theme: parseTheme(read(THEME_KEY)), text: parseTextSize(read(TEXT_KEY)) };
}
export function saveAppearance(theme: ThemePref, text: TextSize): boolean {
  try {
    localStorage.setItem(THEME_KEY, theme);
    localStorage.setItem(TEXT_KEY, text);
    return true;
  } catch {
    return false;
  }
}
export function applyAppearance(theme: ThemePref, text: TextSize): ResolvedTheme {
  const prefersLight =
    typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches;
  const resolved = resolveTheme(theme, prefersLight);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.text = text;
  // The original olive look is untouched; only alternate skins use the override layer.
  root.toggleAttribute("data-alt", resolved !== "olive");
  root.style.colorScheme = resolved === "ivory" ? "light" : "dark";
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLOR[resolved]);
  return resolved;
}
