import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { arrange, newProject } from "./helpers";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

async function seriousViolations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${v.id} (${v.impact}): ${v.help} -> ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join("; ")}`);
}

test.describe("accessibility", () => {
  test("the main pages have no serious or critical axe violations", async ({ page }) => {
    const seen: Record<string, string[]> = {};

    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    seen.home = await seriousViolations(page);
    await page.goto("/#/login");
    await expect(page.getByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
    seen.login = await seriousViolations(page);
    await page.goto("/#/register");
    await expect(page.getByRole("heading", { level: 1, name: "Create your account" })).toBeVisible();
    seen.register = await seriousViolations(page);

    await newProject(page);
    await arrange(page);
    await expect(page.locator(".notation svg").first()).toBeVisible({ timeout: 60_000 });
    seen.arrangement = await seriousViolations(page);
    await page.getByTestId("tab-hands").click();
    seen.hands = await seriousViolations(page);
    await page.goto("/#/library");
    await expect(page.getByTestId("project-link")).toBeVisible();
    seen.library = await seriousViolations(page);
    await page.goto("/#/account");
    await expect(page.getByRole("heading", { level: 1, name: "Account" })).toBeVisible();
    seen.account = await seriousViolations(page);

    const report = Object.entries(seen).filter(([, v]) => v.length).map(([k, v]) => `${k}:\n  ${v.join("\n  ")}`).join("\n");
    expect(report, report).toBe("");
  });
});
