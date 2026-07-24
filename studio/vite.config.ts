import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// The built SPA is vendored into the Python package at src/satay/_studio_assets/,
// where the V5 control server serves it (ADR-0013: prebuilt in CI, shipped in the
// satay[studio] wheel — never built at pip install). `base: "./"` keeps asset URLs
// relative so the bundle works regardless of the path it is mounted at.
export default defineConfig({
  plugins: [svelte()],
  base: "./",
  build: {
    outDir: "../src/satay/_studio_assets",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // Dev-only convenience: proxy read/control API calls to a running V5 server.
    proxy: {
      "/runs": "http://127.0.0.1:8787",
    },
  },
  test: {
    environment: "node",
    globals: true,
    include: ["src/**/*.test.ts"],
  },
});
