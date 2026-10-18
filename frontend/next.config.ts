import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: {
    unoptimized: true,
  },
  async rewrites() {
    // Dev only: proxy to the local API. On Vercel, vercel.json proxies /api/* to the API project.
    if (process.env.NODE_ENV === "development") {
      return [{ source: "/api/:path*", destination: "http://localhost:8000/api/:path*" }];
    }
    return [];
  },
};

export default nextConfig;
