import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/data": "http://127.0.0.1:8000",
      "/system": "http://127.0.0.1:8000",
      "/market": "http://127.0.0.1:8000",
      "/paper": "http://127.0.0.1:8000",
      "/signals": "http://127.0.0.1:8000",
      "/darkflow": "http://127.0.0.1:8000",
      "/shadow-paper": "http://127.0.0.1:8000",
      "/governance": "http://127.0.0.1:8000",
      "/tasks": "http://127.0.0.1:8000",
      "/telegram": "http://127.0.0.1:8000",
      "/backtests": "http://127.0.0.1:8000"
    }
  }
});
