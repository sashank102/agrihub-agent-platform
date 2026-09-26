import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const specDir = path.dirname(fileURLToPath(import.meta.url));

type Keys = {
  user_a: string;
  user_b: string;
  ephemeral: string;
  ephemeral_id: string;
  revoked: string;
  expired: string;
  deleted: string;
};

const root = path.resolve(specDir, "../..");
const composer = (page: Page) =>
  page.getByPlaceholder("Enter a species, trait, and optional SNP list...");

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
  await expect(page.getByLabel("Platform API key")).toBeVisible();
  expect(page.url()).not.toContain("threadId=");
  await expect(page.getByText("Echo: hello")).toHaveCount(0);
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

test("replays a live stream after a mid-run disconnect", async ({ page }) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("slow");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  await page.context().setOffline(true);
  await page.waitForTimeout(500);
  await page.context().setOffline(false);
  await expect(page.getByText("Echo: slow")).toBeVisible({ timeout: 15_000 });
});

test("regenerates from checkpoint history", async ({ page }) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("replay");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: replay")).toBeVisible();
  await page.getByRole("button", { name: "Refresh" }).click({ force: true });
  await expect(page.getByText("Echo: replay").first()).toBeVisible({
    timeout: 15_000,
  });
});

test("edits a human message into a new run", async ({ page }) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("original");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: original")).toBeVisible();
  await page.getByText("original", { exact: true }).hover();
  await page.getByRole("button", { name: "Edit" }).click();
  await page.locator("textarea").first().fill("edited");
  await page.getByRole("button", { name: "Submit" }).click();
  await expect(page.getByText("Echo: edited")).toBeVisible({ timeout: 15_000 });
});

test("rejects an oversized file before send and accepts a small one", async ({
  page,
}) => {
  await signIn(page, keys.user_a);
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "base64",
  );
  await page.locator("#file-input").setInputFiles({
    name: "small.png",
    mimeType: "image/png",
    buffer: png,
  });
  await expect(page.getByTestId("composer-error")).toHaveCount(0);
  await expect(page.getByLabel(/Remove/)).toBeVisible();

  await page.locator("#file-input").setInputFiles({
    name: "big.png",
    mimeType: "image/png",
    buffer: Buffer.alloc(8 * 1024 * 1024, 1),
  });
  await expect(page.getByTestId("composer-error")).toContainText("too large");
  await expect(page.getByLabel(/Remove/)).toHaveCount(1);
});

test("clears tenant state when a key is revoked", async ({ page }) => {
  await signIn(page, keys.ephemeral);
  await composer(page).fill("secret-thread");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: secret-thread")).toBeVisible();
  expect(page.url()).toContain("threadId=");

  execFileSync(
    path.join(root, ".tools/bin/uv"),
    [
      "run",
      "python",
      "-m",
      "agent_platform",
      "revoke-key",
      "--key-id",
      keys.ephemeral_id,
    ],
    {
      cwd: root,
      env: {
        ...process.env,
        DATABASE_URI:
          process.env.DATABASE_URI ||
          "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
        ENVIRONMENT: "test",
        AUTH_MODE: "api_key",
        API_KEY_PEPPER: "playwright-pepper-0123456789",
        API_KEY_PREFIX: "aghub",
      },
    },
  );

  await composer(page).fill("again");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByLabel("Platform API key")).toBeVisible();
  expect(page.url()).not.toContain("threadId=");
  await expect(page.getByText("Echo: secret-thread")).toHaveCount(0);
  await signIn(page, keys.user_b);
  await expect(page.getByText("Echo: secret-thread")).toHaveCount(0);
});

test("restores history after the API restarts", async ({ page }) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("persist");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: persist")).toBeVisible();

  fs.writeFileSync(path.join(specDir, ".restart"), "1");
  await expect
    .poll(async () => {
      try {
        const response = await page.request.get("http://127.0.0.1:8000/health");
        return response.ok();
      } catch {
        return false;
      }
    })
    .toBe(false);
  await expect
    .poll(
      async () => {
        try {
          const response = await page.request.get(
            "http://127.0.0.1:8000/ready",
          );
          return response.ok();
        } catch {
          return false;
        }
      },
      { timeout: 40_000 },
    )
    .toBe(true);

  await page.reload();
  await expect(page.getByText("Echo: persist")).toBeVisible();
  await composer(page).fill("after-restart");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Echo: after-restart")).toBeVisible();
});

test("surfaces an orphaned run as interrupted after restart", async ({
  page,
}) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("hold");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  const threadId = new URL(page.url()).searchParams.get("threadId");
  expect(threadId).toBeTruthy();

  fs.writeFileSync(path.join(specDir, ".restart"), "1");
  await expect
    .poll(async () => {
      try {
        const response = await page.request.get("http://127.0.0.1:8000/health");
        return response.ok();
      } catch {
        return false;
      }
    })
    .toBe(false);
  await expect
    .poll(
      async () => {
        try {
          const response = await page.request.get(
            "http://127.0.0.1:8000/ready",
          );
          return response.ok();
        } catch {
          return false;
        }
      },
      { timeout: 40_000 },
    )
    .toBe(true);

  const search = await page.request.post(
    "http://127.0.0.1:8000/threads/search",
    {
      headers: {
        "X-Api-Key": keys.user_a,
        "Content-Type": "application/json",
      },
      data: { ids: [threadId] },
    },
  );
  expect(search.ok()).toBeTruthy();
  const threads = (await search.json()) as Array<{
    thread_id: string;
    status: string;
  }>;
  expect(threads).toHaveLength(1);
  expect(threads[0]).toMatchObject({
    thread_id: threadId,
    status: "interrupted",
  });
});

test("rejects an interrupt", async ({ page }) => {
  await signIn(page, keys.user_a);
  await composer(page).fill("interrupt");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(
    page.getByRole("button", { name: "Approve" }).first(),
  ).toBeVisible();
  await page
    .getByPlaceholder("Share feedback with the agent...")
    .fill("no thanks");
  await page.getByRole("button", { name: "Submit rejection" }).click();
  await expect(page.getByText(/decision:reject/)).toBeVisible();
});
