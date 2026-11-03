import fs from "node:fs";
import { expect, test } from "@playwright/test";
import { arrange, newProject, register, uploadScore } from "./helpers";

test.describe("core workflow", () => {
  test("a score uploaded as JSON becomes a piece with its facts", async ({ page }) => {
    await register(page);
    await uploadScore(page, "Four Bar Study");
    await expect(page.getByTestId("tab-source")).toHaveAttribute("aria-selected", "true");
    const facts = page.locator("dl.facts").first();
    await expect(facts).toContainText("4 bars");
    await expect(facts).toContainText("120 beats per minute");
    await expect(facts).toContainText("32");   // notes
    // it is in the library too
    await page.goto("/#/library");
    await expect(page.getByTestId("project-link")).toHaveText("Four Bar Study");
    await expect(page.getByTestId("library-summary")).toContainText("Showing 1 to 1 of 1");
  });

  test("the player profile can be edited from a preset or by hand", async ({ page }) => {
    await newProject(page);
    await page.getByTestId("tab-hands").click();
    await page.getByTestId("profile-preset").selectOption("small_hands");
    const reach = page.getByTestId("profile-max_span");
    await expect(reach).toHaveValue(/^(10|9|8)$/);
    await page.getByTestId("profile-preset").selectOption("advanced");
    await expect(reach).toHaveValue("14");
    // A hand-edited value turns the preset into "Custom" and is kept.
    await reach.fill("11");
    await reach.dispatchEvent("change");
    await expect(page.getByTestId("profile-preset")).toHaveValue("");
    await expect(reach).toHaveValue("11");
    // Out-of-range values are refused next to the field.
    await reach.fill("99");
    await reach.dispatchEvent("change");
    await expect(page.locator(".field-error").filter({ hasText: "Enter a number from 1 to 24." })).toBeVisible();
  });

  test("arranging renders and verifies the piece and shows the verdict", async ({ page }) => {
    await newProject(page);
    await arrange(page);
    const verdict = page.getByTestId("verdict");
    await expect(verdict).toBeVisible();
    await expect(verdict).toHaveAttribute("data-status", /^(passes|findings|unresolved)$/);
    await expect(page.getByTestId("fidelity-score")).toHaveText(/Fidelity score \d+%/);
    await expect(page.getByRole("heading", { name: "What was changed" })).toBeVisible();
    // Either a findings table or the sentence saying there are none, never neither.
    await expect(page.getByTestId("findings").or(page.getByTestId("findings-none"))).toBeVisible();
    await expect(page.getByTestId("toast-success").filter({ hasText: "Arrangement ready." })).toBeVisible();
  });

  test("an edited plan can be authored and evaluated into a new revision", async ({ page }) => {
    await newProject(page);
    await arrange(page);
    await page.getByTestId("tab-advanced").click();
    const editor = page.getByTestId("plan-json");
    const plan = JSON.parse(await editor.inputValue());
    expect(Array.isArray(plan.sections) && plan.sections.length > 0).toBe(true);
    for (const section of plan.sections) {
      section.lh_pattern = "pedal_tone";
      section.lh_voices = 1;
    }
    await editor.fill(JSON.stringify(plan, null, 2));
    await page.getByTestId("plan-evaluate").click();
    await expect(page.getByRole("heading", { level: 2, name: "Arrangement 2: edited plan" })).toBeVisible({ timeout: 120_000 });
    await page.getByTestId("tab-advanced").click();
    await expect(page.getByTestId("plan-json")).toHaveValue(/"pedal_tone"/);
    // Invalid JSON is refused without a request.
    await page.getByTestId("plan-json").fill("{ not json");
    await page.getByTestId("plan-evaluate").click();
    await expect(page.locator(".field-error").filter({ hasText: "That is not valid JSON." })).toBeVisible();
  });

  test("the arrangement can be exported as a MIDI file", async ({ page }) => {
    await newProject(page);
    await arrange(page);
    const downloadPromise = page.waitForEvent("download");
    await page.getByTestId("download-midi").click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.mid$/);
    const bytes = fs.readFileSync(await download.path());
    expect(bytes.subarray(0, 4).toString("latin1")).toBe("MThd");
    expect(bytes.length).toBeGreaterThan(100);
  });
});
