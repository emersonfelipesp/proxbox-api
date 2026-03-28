import type { NextConfig } from "next";

const allowedDevOrigins = (process.env.NEXT_ALLOWED_DEV_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean)
  .map((origin) => {
    const normalized = origin.replace(/\/$/, "");

    try {
      return new URL(normalized).hostname;
    } catch {
      try {
        return new URL(`http://${normalized}`).hostname;
      } catch {
        return normalized;
      }
    }
  });

const nextConfig: NextConfig = {
  allowedDevOrigins,
};

export default nextConfig;
