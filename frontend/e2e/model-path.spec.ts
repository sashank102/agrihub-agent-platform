import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const specDir = path.dirname(fileURLToPath(import.meta.url));
const shots =
  process.env.SCREENSHOT_DIR ||
  path.join(specDir, "../test-results/screenshots");
const keys = JSON.parse(
  fs.readFileSync(path.join(specDir, ".keys.json"), "utf8"),
) as { user_a: string };

async function signIn(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Platform API key").fill(keys.user_a);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("heading", { name: "Studies" })).toBeVisible();
}

async function newStudy(page: Page, trait: string): Promise<void> {
  await signIn(page);
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "New study" })
    .click();
  await page.getByRole("combobox", { name: "Trait" }).fill(trait);
  await page.keyboard.press("Escape");
}

async function shot(page: Page, name: string): Promise<void> {
  fs.mkdirSync(shots, { recursive: true });
  await page.screenshot({ path: path.join(shots, name), fullPage: true });
}

test("a poster study answers a follow-up question with cited evidence", async ({
  page,
}) => {
  await newStudy(page, "plant height");
  await page.getByRole("button", { name: "Use the poster SNPs" }).click();
  await expect(page.getByTestId("server-preview")).toContainText("3 loci");
  await page.getByRole("button", { name: "Start study" }).click();
  await page.waitForURL(/\/studies\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId("run-view")).toHaveAttribute(
    "data-collapsed",
    "true",
    { timeout: 180_000 },
  );
  await page.getByTestId("open-qa").click();
  const panel = page.getByTestId("qa-panel");
  const question = "why is Glyma.18G092200 a candidate?";
  await expect(async () => {
    const input = panel.getByLabel("Follow-up question");
    if ((await input.inputValue()) !== question) {
      await input.fill(question);
    }
    await panel.getByRole("button", { name: "Ask" }).click();
    await expect(panel).toContainText(question, { timeout: 5_000 });
  }).toPass({ timeout: 60_000 });
  await expect(panel).toContainText(/Glyma\.18G092200 .*\[E\d+\]/, {
    timeout: 60_000,
  });
  await expect(panel).not.toContainText("no stored alias");
  await shot(page, "a-poster-followup.png");
});

test("a trait study runs the sister-team PH_2019 results through the pipeline", async ({
  page,
}) => {
  await newStudy(page, "plant height");
  await page.getByText("Species + trait").click();
  const models = page.getByTestId("model-panel");
  await expect(models).toContainText("Sister-team kinship-graph GNN", {
    timeout: 30_000,
  });
  await models
    .getByRole("radio", { name: /Sister-team kinship-graph GNN.*PH_2019/ })
    .click();
  await expect(models).toContainText(
    "positions on Lee.gnm2 are lifted to the study assembly",
  );
  await shot(page, "b-trait-form.png");
  await page.getByRole("button", { name: "Start study" }).click();
  await page.waitForURL(/\/studies\/[0-9a-f-]{36}$/);
  await expect(page.getByTestId("run-view")).toHaveAttribute(
    "data-collapsed",
    "true",
    { timeout: 300_000 },
  );
  const report = page.getByTestId("report-view");
  await report.getByRole("tab", { name: "Summary" }).click();
  await expect(report).toContainText("Model step");
  await expect(report).toContainText("PH_2019");
  await shot(page, "b-trait-report.png");
  await page.getByRole("button", { name: "Open full trace" }).click();
  const decision = page.locator('[data-kind="select_model"]');
  await expect(decision).toContainText("model choice");
  await expect(decision).toContainText("gnnexplainer");
  await page.mouse.move(0, 0);
  await decision.focus();
  await expect(page.getByRole("dialog", { name: /specialist/ })).toHaveCount(0);
  await shot(page, "b-trait-model-choice.png");
  await decision.screenshot({
    path: path.join(shots, "b-trait-model-choice-decision.png"),
  });
});

test("a trait without a registered model shows the no-model state", async ({
  page,
}) => {
  await newStudy(page, "nodule colour");
  await page.getByText("Species + trait").click();
  await expect(page.getByTestId("no-model")).toContainText(
    "No applicable model registered",
    { timeout: 30_000 },
  );
  await expect(
    page.getByRole("button", { name: "Start study" }),
  ).toBeDisabled();
  await shot(page, "c-no-model.png");
});
