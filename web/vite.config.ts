import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into ../docs so GitHub Pages can serve it from the main branch
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../docs", emptyOutDir: true, chunkSizeWarningLimit: 2000 },
});
