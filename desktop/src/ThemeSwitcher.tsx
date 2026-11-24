import { useEffect, useRef, useState } from "react";
import { Palette } from "lucide-react";
import {
  THEMES,
  TEXT_SIZES,
  applyAppearance,
  loadAppearance,
  saveAppearance,
  type TextSize,
  type ThemePref,
} from "./theme";

const LABELS: Record<ThemePref, [string, string]> = {
  olive: ["Olive dark", "Default"],
  terminal: ["Terminal", "Neutral, one accent"],
  ivory: ["Ivory light", "Daylight"],
  system: ["System", "Follow your OS"],
};

export default function ThemeSwitcher() {
  const initial = useRef(loadAppearance()).current;
  const [theme, setTheme] = useState<ThemePref>(initial.theme);
  const [text, setText] = useState<TextSize>(initial.text);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    applyAppearance(theme, text);
    if (theme !== "system" || typeof matchMedia !== "function") return;
    const query = matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyAppearance(theme, text);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [theme, text]);

  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, [open]);

  const choose = (nextTheme: ThemePref, nextText: TextSize) => {
    setTheme(nextTheme);
    setText(nextText);
    saveAppearance(nextTheme, nextText);
  };

  return (
    <div className="appearance" ref={box}>
      {open && (
        <div className="appearance-panel" role="group" aria-label="Appearance settings">
          <span className="small-cap">COLOUR THEME</span>
          <div className="appearance-options" role="radiogroup" aria-label="Colour theme">
            {THEMES.map((t) => (
              <button
                key={t}
                role="radio"
                aria-checked={theme === t}
                className={`appearance-option ${theme === t ? "active" : ""}`}
                onClick={() => choose(t, text)}
              >
                <span className="appearance-swatch" data-swatch={t} aria-hidden="true" />
                <span>
                  <strong>{LABELS[t][0]}</strong>
                  <small>{LABELS[t][1]}</small>
                </span>
              </button>
            ))}
          </div>
          <span className="small-cap">TEXT SIZE</span>
          <div className="appearance-sizes" role="radiogroup" aria-label="Text size">
            {TEXT_SIZES.map((s) => (
              <button
                key={s}
                role="radio"
                aria-checked={text === s}
                className={text === s ? "active" : ""}
                onClick={() => choose(theme, s)}
              >
                {s}%
              </button>
            ))}
          </div>
        </div>
      )}
      <button
        className="appearance-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Palette size={15} /> Appearance
      </button>
    </div>
  );
}
