import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // For project-Pages deploy at <user>.github.io/<repo>/ the workflow sets BASE_PATH=/<repo>/.
  // Local dev / user-Pages / custom-domain default to "/".
  base: process.env.BASE_PATH ?? "/",
  server: {
    proxy: {
      "/auth": "http://localhost:8000",
      "/corpus": "http://localhost:8000",
      "/eras": "http://localhost:8000",
      "/notes": "http://localhost:8000",
      "/import": "http://localhost:8000",
      "/draft": "http://localhost:8000",
      "/promote": "http://localhost:8000",
      "/samples": "http://localhost:8000",
      "/themes-spin": "http://localhost:8000",
      "/session": { target: "ws://localhost:8000", ws: true },
      "/themes-curate": { target: "ws://localhost:8000", ws: true },
    },
  },
});
