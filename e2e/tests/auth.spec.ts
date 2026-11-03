import { expect, test } from "@playwright/test";
import { PASSWORD, login, register, uniqueEmail } from "./helpers";

test.describe("authentication", () => {
  test("a new account can be registered and lands in an empty library", async ({ page }) => {
    await register(page);
    await expect(page.getByTestId("library-summary")).toHaveText("No pieces yet. Upload one to begin.");
    await expect(page.getByTestId("nav-sign-out")).toBeVisible();
  });

  test("an existing account can sign in", async ({ page, context }) => {
    const email = await register(page);
    await context.clearCookies();
    await page.reload();
    await login(page, email);
    await expect(page.getByRole("heading", { level: 1, name: "Your pieces" })).toBeVisible();
    await expect(page.getByTestId("nav-account")).toBeVisible();
  });

  test("signing out ends the session", async ({ page }) => {
    await register(page);
    await page.getByTestId("nav-sign-out").click();
    await expect(page.getByTestId("nav-login")).toBeVisible();
    await expect(page.getByTestId("toast-success").filter({ hasText: "Signed out." })).toBeVisible();
    await page.goto("/#/library");
    await expect(page).toHaveURL(/#\/login$/);
  });

  test("a wrong password is refused in words and stays on the sign-in form", async ({ page, context }) => {
    const email = await register(page);
    await context.clearCookies();
    await page.reload();   // the app re-checks the session on load; a hash change alone keeps its memory
    await login(page, email, "definitely-not-the-password");
    const error = page.getByTestId("auth-error");
    await expect(error).toBeVisible();
    await expect(error).not.toHaveText("");
    await expect(error).not.toContainText("Traceback");
    await expect(page).toHaveURL(/#\/login$/);
    await expect(page.getByTestId("nav-sign-out")).toHaveCount(0);
  });

  test("protected routes redirect a visitor to sign in", async ({ page }) => {
    for (const route of ["/#/library", "/#/account", "/#/project/not-a-real-id"]) {
      await page.goto(route);
      await expect(page).toHaveURL(/#\/login$/);
      await expect(page.getByRole("heading", { level: 1, name: "Sign in" })).toBeVisible();
    }
    // and an unknown account cannot get in
    await login(page, uniqueEmail("nobody"), PASSWORD);
    await expect(page.getByTestId("auth-error")).toBeVisible();
  });
});
