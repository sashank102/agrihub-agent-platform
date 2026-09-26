import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const root = path.resolve(frontendDir, "..");
const databaseUri =
  process.env.DATABASE_URI ||
  "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform";

const env = {
  ...process.env,
  DATABASE_URI: databaseUri,
  ENVIRONMENT: "test",
  AUTH_MODE: "api_key",
  API_KEY_PEPPER: "playwright-pepper-0123456789",
  API_KEY_PREFIX: "aghub",
  GRAPH_FIXTURE: "true",
  UV_PYTHON_INSTALL_DIR: path.join(root, ".uv-python"),
  UV_PYTHON_BIN_DIR: path.join(root, ".uv-python/bin"),
  UV_CACHE_DIR: path.join(root, ".uv-cache"),
};

export default function globalSetup(): void {
  const uv = path.join(root, ".tools/bin/uv");
  execFileSync(uv, ["run", "alembic", "upgrade", "head"], {
    cwd: root,
    env,
    stdio: "inherit",
  });
  execFileSync(uv, ["run", "python", "frontend/e2e/provision.py"], {
    cwd: root,
    env,
    stdio: "inherit",
  });
}
