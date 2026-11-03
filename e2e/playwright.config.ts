// Playwright configuration. `webServer` starts the real API against a fresh
// SQLite database under e2e/.tmp, so every run begins with no accounts and no
// pieces; the tests register what they need.
//
// The API is started with the repository's Python: ARRANGER_PYTHON overrides
// it, otherwise the project's .venv is used when present, otherwise `python`.
import { defineConfig, devices } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const tmp = path.join(__dirname, ".tmp");
const port = Number(process.env.E2E_PORT || 8766);
const baseURL = `http://127.0.0.1:${port}`;

fs.mkdirSync(tmp, { recursive: true });
// Playwright evaluates this file in the runner and again in every worker
// process. Only the runner, which starts the server, may reset the database;
// a worker doing it later would pull the file out from under a live server.
// Workers inherit the runner's environment, so the flag marks them.
if (!process.env.ARRANGER_E2E_RUNNER) {
  process.env.ARRANGER_E2E_RUNNER = String(process.pid);
  for (const name of ["app.db", "app.db-wal", "app.db-shm"]) {
    fs.rmSync(path.join(tmp, name), { force: true });
  }
}

function pythonInterpreter(): string {
  if (process.env.ARRANGER_PYTHON) return process.env.ARRANGER_PYTHON;
  for (const candidate of [
    path.join(root, ".venv", "Scripts", "python.exe"),
    path.join(root, ".venv", "bin", "python"),
  ]) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return "python";
}

export default defineConfig({
  testDir: "./tests",
  // One server and one SQLite file: tests run one at a time, each with its own account.
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `"${pythonInterpreter()}" -m uvicorn arranger_api.main:app --host 127.0.0.1 --port ${port} --log-level warning`,
    url: `${baseURL}/health`,
    cwd: root,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // The API logs JSON lines to stderr; keep them in the test output so a 500 is explained.
    stdout: "pipe",
    stderr: "pipe",
    env: {
      ...process.env,
      APP_ENV: "development",
      PORT: String(port),
      PUBLIC_BASE_URL: baseURL,
      EMAIL_PROVIDER: "console",
      COOKIE_SECURE: "false",
      CSRF_PROTECTION: "true",
      REQUIRE_VERIFIED_EMAIL: "false",
      JOB_WORKERS: "1",
      ARRANGER_DB_PATH: path.join(tmp, "app.db"),
      ARTIFACT_BACKEND: "local",
      ARTIFACT_DIR: path.join(tmp, "files"),
      RATE_LIMIT_BACKEND: "memory",
      // The suite registers an account per test from one address; the
      // production limits would refuse it as a flood.
      AUTH_RATE_LIMIT_REQUESTS: "1000",
      RATE_LIMIT_REQUESTS: "5000",
      READ_RATE_LIMIT_REQUESTS: "10000",
      UPLOAD_RATE_LIMIT_REQUESTS: "1000",
      LOG_LEVEL: "WARNING",
    },
  },
});
