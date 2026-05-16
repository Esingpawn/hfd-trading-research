import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";
const apiProxy = { target: apiTarget, changeOrigin: true, secure: false };

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": apiProxy,
      "/data": apiProxy,
      "/system": apiProxy,
      "/market": apiProxy,
      "/paper": apiProxy,
      "/signals": apiProxy,
      "/darkflow": apiProxy,
      "/shadow-paper": apiProxy,
      "/governance": apiProxy,
      "/tasks": apiProxy,
      "/telegram": apiProxy,
      "/backtests": apiProxy,
      "/trading": apiProxy
    }
  }
});
