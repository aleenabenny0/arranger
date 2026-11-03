import { expect, test } from "@playwright/test";
import { arrange, newProject } from "./helpers";

const TABS = ["source", "hands", "arrange", "revisions", "advanced"] as const;

test.describe("views", () => {
  test("the step tabs switch panels and keep the WAI-ARIA state", async ({ page }) => {
    await newProject(page);
    for (const id of TABS) {
      await page.getByTestId(`tab-${id}`).click();
      await expect(page.getByTestId(`tab-${id}`)).toHaveAttribute("aria-selected", "true");
      await expect(page.getByTestId(`panel-${id}`)).toBeVisible();
      for (const other of TABS.filter((t) => t !== id)) {
        await expect(page.getByTestId(`tab-${other}`)).toHaveAttribute("aria-selected", "false");
        await expect(page.getByTestId(`panel-${other}`)).toBeHidden();
      }
    }
    // Before any arrangement the Advanced view says what to do first.
    await expect(page.getByTestId("panel-advanced")).toContainText("Make an arrangement first.");
  });

  test("the score, plan and JSON views show the arrangement", async ({ page }) => {
    await newProject(page);
    await arrange(page);
    // Score: real notation drawn from the MusicXML the server produced.
    await page.getByTestId("tab-arrange").click();
    await expect(page.locator(".notation svg g.note").first()).toBeVisible({ timeout: 60_000 });
    expect(await page.locator(".notation svg g.note").count()).toBeGreaterThanOrEqual(16);
    // Plan: the decisions, as editable JSON.
    await page.getByTestId("tab-advanced").click();
    await expect(page.getByTestId("plan-json")).toHaveValue(/"sections"/);
    // JSON: the verdict and the engine report, verbatim.
    await page.getByTestId("json-verdict").locator("summary").click();
    await expect(page.getByTestId("json-verdict").locator("pre")).toContainText('"playable"');
    await page.getByTestId("json-report").locator("summary").click();
    await expect(page.getByTestId("json-report").locator("pre")).toContainText("{");
    // Revisions: the arrangement is listed with its verdict.
    await page.getByTestId("tab-revisions").click();
    const rows = page.getByTestId("revisions").locator("tbody tr");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText(/Passes|beyond limits/);
  });
});
