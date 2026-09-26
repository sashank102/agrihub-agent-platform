import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const specDir = path.dirname(fileURLToPath(import.meta.url));

type Keys = {
  user_a: string;
  user_b: string;
  revoked: string;
  expired: string;
  deleted: string;
};

const keys = JSON.parse(
  fs.readFileSync(path.join(specDir, ".keys.json"), "utf8"),
) as Keys;

async function signIn(page: Page, apiKey: string): Promise<void> {
  await page.goto("/");
  await page.getByLabel("Platform API key").fill(apiKey);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(
    page.getByPlaceholder("Enter a species, trait, and optional SNP list..."),
  ).toBeVisible();
}

test("requires a platform key before showing the chat", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByLabel("Platform API key")).toBeVisible();
  await expect(
    page.getByPlaceholder("Enter a species, trait, and optional SNP list..."),
  ).toHaveCount(0);
  expect(page.url()).not.toContain("apiKey");
});

test("rejects invalid, revoked, expired, and deleted credentials", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Platform API key")
    .fill("aghub_notarealkey0123456789abc");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByRole("status")).toContainText("rejected");

  for (const key of [keys.revoked, keys.expired, keys.deleted]) {
    await page.getByLabel("Platform API key").fill(key);
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.getByRole("status")).toContainText("rejected");
    expect(page.url()).not.toContain(key);
  }
});

test("streams a reply, keeps it after reload, and hides it from another user", async ({
  page,
}) => {
  await signIn(page, keys.user_a);
  await page
    .getByPlaceholder("Enter a species, trait, and optional SNP list...")
    .fill("hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: hello")).toBeVisible();
  const threadUrl = page.url();
  expect(threadUrl).toContain("threadId=");

  await page.reload();
  await expect(page.getByText("Echo: hello")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, keys.user_b);
  await expect(page.getByText("Echo: hello")).toHaveCount(0);
  await page.goto(threadUrl);
  await expect(page.getByText("Echo: hello")).toHaveCount(0);
  await expect(page.getByText(keys.user_a)).toHaveCount(0);
});

test("cancels a slow run", async ({ page }) => {
  await signIn(page, keys.user_a);
  const composer = page.getByPlaceholder(
    "Enter a species, trait, and optional SNP list...",
  );
  await composer.fill("slow");
  await page.getByRole("button", { name: "Send" }).click();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
  await expect(page.getByText("Echo: slow")).toHaveCount(0);
});

test("approves an interrupt", async ({ page }) => {
  await signIn(page, keys.user_a);
  await page
    .getByPlaceholder("Enter a species, trait, and optional SNP list...")
    .fill("interrupt");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(
    page.getByRole("button", { name: "Approve" }).first(),
  ).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).first().click();
  await expect(page.getByText(/decision:approve/)).toBeVisible();
});

test("accepts a small image and rejects an oversized request", async ({
  page,
  request,
}) => {
  await signIn(page, keys.user_a);
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "base64",
  );
  await page.locator("#file-input").setInputFiles({
    name: "dot.png",
    mimeType: "image/png",
    buffer: png,
  });
  await page
    .getByPlaceholder("Enter a species, trait, and optional SNP list...")
    .fill("see image");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/blocks:/)).toBeVisible();

  const oversized = await request.post("http://127.0.0.1:8000/threads/search", {
    headers: {
      "X-Api-Key": keys.user_a,
      "Content-Type": "application/json",
    },
    data: "x".repeat(10_485_761),
  });
  expect(oversized.status()).toBe(413);
  expect(oversized.statusText()).not.toContain(keys.user_a);
});
