import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const specDir = path.dirname(fileURLToPath(import.meta.url));
const keys = JSON.parse(
  fs.readFileSync(path.join(specDir, ".keys.json"), "utf8"),
) as { user_a: string; user_b: string };

async function signIn(page: Page, apiKey: string): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Platform API key").fill(apiKey);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Studies" })).toBeVisible();
}

async function openForm(page: Page): Promise<void> {
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", {
      name: "New study",
    })
    .click();
  await expect(page.getByRole("heading", { name: "New study" })).toBeVisible();
  await page.getByRole("combobox", { name: "Trait" }).fill("plant height");
  await page.keyboard.press("Escape");
}

test("runs the poster study from the form to the collapsed report and replays it", async ({
  page,
}) => {
  await signIn(page, keys.user_a);
  await openForm(page);
  await page.getByRole("button", { name: "Use the poster SNPs" }).click();
  await expect(page.getByTestId("snp-summary")).toHaveText(
    "3 valid · 0 duplicates · 0 invalid · 0 need lookup",
  );
  await expect(page.getByTestId("server-preview")).toContainText(
    "3 loci · 123 candidate genes · ±250 kb windows",
  );

  await page.getByRole("button", { name: "Start study" }).click();
  await page.waitForURL(/\/studies\/[0-9a-f-]{36}$/);
  const studyUrl = page.url();
  await expect(page.getByTestId("harvest-lane")).toBeVisible();

  const view = page.getByTestId("run-view");
  await expect(view).toHaveAttribute("data-collapsed", "true", {
    timeout: 120_000,
  });
  await expect(page.locator("[data-phase]")).toHaveCount(7);
  for (const phase of await page.locator("[data-phase]").all()) {
    await expect(phase).toHaveAttribute("data-state", "done");
  }
  const trace = page.getByTestId("research-trace");
  await expect(trace).toContainText("5 agents · harvest 100%");
  await expect(trace.locator('li[data-status="completed"]')).toHaveCount(5);
  await expect(page.getByTestId("positional-only")).toBeVisible();
  const wrky = page.locator('[data-gene="Glyma.18G092200"]');
  await expect(wrky).toContainText("Gm18:9,262,392-9,267,008");
  await expect(wrky).toContainText("overlaps SNP");

  await page.getByRole("button", { name: "Open full trace" }).click();
  const sheet = page.getByRole("dialog");
  await sheet.locator('li[data-kind="dispatch"]').hover();
  await expect(sheet.locator("article[data-highlighted]")).toHaveCount(5);
  await page.keyboard.press("Escape");

  await page.reload();
  await expect(view).toHaveAttribute("data-collapsed", "true", {
    timeout: 30_000,
  });
  await expect(trace.locator('li[data-status="completed"]')).toHaveCount(5);
  await expect(wrky).toContainText("overlaps SNP");

  await page.getByRole("link", { name: "Studies" }).click();
  const card = page.locator(`a[href="${new URL(studyUrl).pathname}"]`);
  await expect(card).toContainText("plant height");
  await expect(card).toContainText("Completed");
  await expect(card).toContainText("3 SNPs");

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, keys.user_b);
  await page.goto(studyUrl);
  await expect(
    page.getByRole("heading", { name: "Study not found" }),
  ).toBeVisible();
});

test("shows the server's placement errors in the form preview", async ({
  page,
}) => {
  await signIn(page, keys.user_a);
  await openForm(page);
  await page
    .getByRole("textbox", { name: "SNP list" })
    .fill("BARC_123\nss0000001\nS99_5");
  const preview = page.getByTestId("server-preview");
  await expect(preview.getByRole("alert")).toContainText(
    "none of the 2 SNPs could be placed on Wm82.a2.v1",
  );
  await expect(page.locator('[role=row][data-status="invalid"]')).toHaveCount(
    3,
  );
  await expect(page.getByTestId("snp-summary")).toHaveText(
    "0 valid · 0 duplicates · 3 invalid · 0 need lookup",
  );
});
