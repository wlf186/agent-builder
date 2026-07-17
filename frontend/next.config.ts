import type { NextConfig } from "next";
import path from "node:path";

const docsHost = process.env.DOCS_HOST || '127.0.0.1';
const docsPort = process.env.DOCS_PORT || '4173';

const nextConfig: NextConfig = {
  turbopack: {
    root: path.resolve(__dirname),
  },
  async rewrites() {
    return [
      // Docs-site proxy (VitePress 使用 base: '/docs/'，所以目标路径也需要包含 /docs)
      {
        source: '/docs/:path*',
        destination: `http://${docsHost}:${docsPort}/docs/:path*`,
      },
      {
        source: '/docs',
        destination: `http://${docsHost}:${docsPort}/docs`,
      },
    ];
  },
};

export default nextConfig;
