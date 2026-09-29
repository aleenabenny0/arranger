// Shared steps. Every test registers its own account so tests never share state.
import { expect, type Page } from "@playwright/test";

export const PASSWORD = "correct-horse-battery-7";

let counter = 0;
export function uniqueEmail(prefix = "e2e"): string {
  counter += 1;
  return `${prefix}-${Date.now()}-${process.pid}-${counter}@example.com`;
}

// A four-bar study in 4/4 at 120 bpm: a tune on staff 1 over a bass on staff 2.
// The same shape the API's score JSON accepts (see adapters/score_json.py).
export function scoreJson(title = "E2E Study"): string {
  const beat = 0.5;
  const tune = [72, 74, 76, 77, 79, 77, 76, 74];
  const roots = [48, 53, 55, 48];
  const notes: object[] = [];
  for (let bar = 0; bar < 4; bar += 1) {
    for (let k = 0; k < 4; k += 1) {
      const onset = (bar * 4 + k) * beat;
      notes.push({ pitch: tune[(bar * 4 + k) % tune.length], onset, duration: beat * 0.95, bar: bar + 1, staff: 1 });
      notes.push({ pitch: roots[bar] + [0, 7, 4, 7][k], onset, duration: beat * 0.95, bar: bar + 1, staff: 2 });
    }
  }
  return JSON.stringify({ title, tempo_bpm: 120, notes });
}

export async function register(page: Page, email = uniqueEmail()): Promise<string> {
  await page.goto("/#/register");
  await page.getByTestId("auth-email").fill(email);
  await page.getByTestId("auth-password").fill(PASSWORD);
  await page.getByTestId("auth-submit").click();
  await expect(page.getByRole("heading", { level: 1, name: "Your pieces" })).toBeVisible();
  return email;
}

export async function login(page: Page, email: string, password = PASSWORD): Promise<void> {
  await page.goto("/#/login");
  await page.getByTestId("auth-email").fill(email);
  await page.getByTestId("auth-password").fill(password);
  await page.getByTestId("auth-submit").click();
}

export async function uploadScore(page: Page, title = "E2E Study"): Promise<void> {
  await page.getByTestId("upload-input").setInputFiles({
    name: "study.json",
    mimeType: "application/json",
    buffer: Buffer.from(scoreJson(title), "utf-8"),
  });
  await expect(page.getByTestId("project-title")).toHaveText(title);
}

// Register, upload a piece, and land on its page.
export async function newProject(page: Page, title = "E2E Study"): Promise<string> {
  const email = await register(page);
  await uploadScore(page, title);
  return email;
}

export async function arrange(page: Page, revision = 1): Promise<void> {
  await page.getByTestId("tab-arrange").click();
  await page.getByTestId("arrange-button").click();
  await expect(page.getByRole("heading", { level: 2, name: `Arrangement ${revision}` })).toBeVisible({ timeout: 120_000 });
}
