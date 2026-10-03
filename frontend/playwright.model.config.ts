import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(frontendDir, "..");
const apiPort = process.env.MODEL_E2E_API_PORT || "8012";
const webPort = process.env.MODEL_E2E_WEB_PORT || "3012";
const apiUrl = `http://127.0.0.1:${apiPort}`;
const webUrl = `http://127.0.0.1:${webPort}`;
const databaseUri =
  process.env.DATABASE_URI ||
  "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform";
const chromium = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "model-path.spec.ts",
  timeout: 420_000,
  fullyParallel: false,
  workers: 1,
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: webUrl,
    trace: "retain-on-failure",
    launchOptions: chromium ? { executablePath: chromium } : {},
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "bash frontend/e2e/study-api-server.sh",
      cwd: root,
      env: {
        ...process.env,
        DATABASE_URI: databaseUri,
        ENVIRONMENT: "test",
        AUTH_MODE: "api_key",
        API_KEY_PEPPER: "playwright-pepper-0123456789",
        API_KEY_PREFIX: "aghub",
        GRAPH_FIXTURE: "true",
        ORCHESTRATOR_MODEL: "agrihub-fake:poster",
        SPECIALIST_MODEL: "agrihub-fake:poster",
        VERIFIER_MODEL: "agrihub-fake:poster",
        WRITER_MODEL: "agrihub-fake:poster",
        QA_MODEL: "agrihub-fake:poster",
        API_HOST: "127.0.0.1",
        API_PORT: apiPort,
        API_ALLOWED_ORIGINS: JSON.stringify([webUrl]),
        UV_PYTHON_INSTALL_DIR: path.join(root, ".uv-python"),
        UV_PYTHON_BIN_DIR: path.join(root, ".uv-python/bin"),
        UV_CACHE_DIR: path.join(root, ".uv-cache"),
      },
      url: `${apiUrl}/ready`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `./node_modules/.bin/next dev --port ${webPort}`,
      cwd: frontendDir,
      env: {
        ...process.env,
        NEXT_PUBLIC_API_URL: apiUrl,
        NEXT_PUBLIC_ASSISTANT_ID: "agrihub_study",
        NEXT_PUBLIC_CHAT_ASSISTANT_ID: "agrihub",
      },
      url: webUrl,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
