import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import OperatorBoundary from "./OperatorBoundary";
import "./tokens.css";
import "./styles.css";
import "./styles/quality.css";
import "./styles/persistence.css";
import "./styles/workspace-studio.css";
import "./styles/risk.css";
import "./themes.css";
import ThemeSwitcher from "./ThemeSwitcher";
import { applyAppearance, loadAppearance } from "./theme";
const appearance = loadAppearance();
applyAppearance(appearance.theme, appearance.text);
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <OperatorBoundary>
      <App />
    </OperatorBoundary>
  </StrictMode>,
);
const appearanceHost = document.createElement("div");
document.body.appendChild(appearanceHost);
createRoot(appearanceHost).render(<ThemeSwitcher />);
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
