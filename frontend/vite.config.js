import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  server: {
    host: true,
    port: 5174,
    // Allow Cloudflare Tunnel development URLs.
    allowedHosts: [".trycloudflare.com"],
    proxy: {
      "/v1": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
