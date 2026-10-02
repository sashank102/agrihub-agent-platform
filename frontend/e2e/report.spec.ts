import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const specDir = path.dirname(fileURLToPath(import.meta.url));
const shots = path.resolve(specDir, "../../../plan7-manual/screenshots");
const keys = JSON.parse(
  fs.readFileSync(path.join(specDir, ".keys.json"), "utf8"),
) as { user_a: string };

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Platform API key").fill(keys.user_a);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Studies" })).toBeVisible();
}

test("runs a study into the report, exports it, and asks one question", async ({
  page,
}) => {
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "New study" })
    .click();
  await page.getByRole("combobox", { name: "Trait" }).fill("plant height");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Use the poster SNPs" }).click();
  await expect(page.getByTestId("snp-summary")).toHaveText(
    "3 valid · 0 duplicates · 0 invalid · 0 need lookup",
  );
  await expect(page.getByTestId("server-preview")).toContainText(
    "3 loci · 123 candidate genes",
  );
  await page.getByRole("button", { name: "Start study" }).click();
  await page.waitForURL(/\/studies\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId("run-view")).toHaveAttribute(
    "data-collapsed",
    "true",
    { timeout: 180_000 },
  );

  const report = page.getByTestId("report-view");
  await expect(report).toBeVisible();
  fs.mkdirSync(shots, { recursive: true });
  await report.getByRole("tab", { name: "Summary" }).click();
  await expect(report).toContainText("candidate genes");
  await page.screenshot({
    path: path.join(shots, "summary.png"),
    fullPage: true,
  });
  await report.getByRole("tab", { name: "Candidates" }).click();
  await expect(page.locator('[data-gene="Glyma.18G092200"]')).toBeVisible();
  await page.screenshot({
    path: path.join(shots, "candidates.png"),
    fullPage: true,
  });
  await report.getByRole("tab", { name: "Evidence" }).click();
  await expect(page.getByTestId("evidence-matrix")).toBeVisible();
  await page.screenshot({
    path: path.join(shots, "evidence-matrix.png"),
    fullPage: true,
  });
  await report.getByRole("tab", { name: "Sources" }).click();
  await page.screenshot({
    path: path.join(shots, "sources.png"),
    fullPage: true,
  });

  const downloadPromise = page.waitForEvent("download");
  await report.getByRole("button", { name: "JSON" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toContain(".json");

  await page.getByTestId("open-qa").click();
  const panel = page.getByTestId("qa-panel");
  await panel
    .getByLabel("Follow-up question")
    .fill("why is Glyma.18G092200 a candidate?");
  await panel.getByRole("button", { name: "Ask" }).click();
  await expect(panel).toContainText(/\[E\d+\]/, { timeout: 60_000 });
  await page.screenshot({ path: path.join(shots, "qa.png"), fullPage: true });
});
