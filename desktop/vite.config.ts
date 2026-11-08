import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig(() => {
  if (process.env.AITOS_CONSOLE_TOKEN) {
    throw new Error(
      "Do not forward a runtime bearer through the public development proxy. Build the desktop bundle and use the authenticated operator gateway instead.",
    );
  }
  return {
    plugins: [react()],
    server: {
      host: "0.0.0.0",
      allowedHosts: true,
      proxy: {
        "/console": {
          target: process.env.AITOS_CONSOLE_TARGET || "http://127.0.0.1:8765",
          changeOrigin: true,
          configure(proxy) {
            proxy.on("proxyReq", (request) => {
              request.removeHeader("authorization");
              request.removeHeader("proxy-authorization");
              request.removeHeader("cookie");
            });
          },
        },
      },
    },
  };
});
