import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: {
    unoptimized: true,
  },
  async rewrites() {
    // Only use rewrites in dev mode, production uses nginx
    if (process.env.NODE_ENV === "development") {
      return [{ source: "/api/:path*", destination: "http://localhost:8000/api/:path*" }];
    }
    return [];
  },
};

export default nextConfig;
