import type { NextConfig } from "next";
import path from "node:path";

const config: NextConfig = {
  output: "export",
  trailingSlash: true,
  generateBuildId: async () => process.env.OMS_BUILD_ID || "oms-community-1.2.0",
  transpilePackages: ["@inogen/oms-client", "@inogen/oms-ui-core"],
  devIndicators: false,
  poweredByHeader: false,
  // The only shared code is in the adjacent public packages directory.
  outputFileTracingRoot: path.resolve(__dirname, ".."),
};

export default config;
