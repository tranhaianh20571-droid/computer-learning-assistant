import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import runtimeAssets from "./runtimeAssets.mjs";
import boardFonts from "./boardFonts.mjs";

export default defineConfig({
  plugins: [react(),runtimeAssets(),boardFonts()],
  server: {
    port: 5173,
    watch: { ignored: ["**/*.tmpdir/**"] },
    proxy: {
      "/api": process.env.TEST_BACKEND || "http://127.0.0.1:8000",
      "/tasks": process.env.TEST_BACKEND || "http://127.0.0.1:8000",
    },
  },
});
