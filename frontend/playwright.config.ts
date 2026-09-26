import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(frontendDir, "..");
const databaseUri =
  process.env.DATABASE_URI ||
  "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform";
const apiEnv = {
  ...process.env,
  DATABASE_URI: databaseUri,
  ENVIRONMENT: "test",
  AUTH_MODE: "api_key",
  API_KEY_PEPPER: "playwright-pepper-0123456789",
  API_KEY_PREFIX: "aghub",
  GRAPH_FIXTURE: "true",
  API_HOST: "127.0.0.1",
  API_PORT: "8000",
  UV_PYTHON_INSTALL_DIR: path.join(root, ".uv-python"),
  UV_PYTHON_BIN_DIR: path.join(root, ".uv-python/bin"),
  UV_CACHE_DIR: path.join(root, ".uv-cache"),
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: "http://127.0.0.1:3000",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "bash frontend/e2e/api-server.sh",
      cwd: root,
      env: apiEnv,
      url: "http://127.0.0.1:8000/ready",
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "./node_modules/.bin/next dev --port 3000",
      cwd: frontendDir,
      env: {
        ...process.env,
        NEXT_PUBLIC_API_URL: "http://127.0.0.1:8000",
        NEXT_PUBLIC_ASSISTANT_ID: "agrihub",
      },
      url: "http://127.0.0.1:3000",
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
