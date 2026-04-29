import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/eras": "http://localhost:8000",
      "/notes": "http://localhost:8000",
      "/draft": "http://localhost:8000",
      "/promote": "http://localhost:8000",
      "/session": { target: "ws://localhost:8000", ws: true },
    },
  },
});
