import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import OperatorBoundary from "./OperatorBoundary";
import "./styles.css";
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <OperatorBoundary>
      <App />
    </OperatorBoundary>
  </StrictMode>,
);
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
