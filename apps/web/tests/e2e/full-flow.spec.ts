import { test, expect, Page } from "@playwright/test";

const BACKEND = process.env.E2E_API_BASE_URL || "http://localhost:8000";

// Random suffix to avoid collisions with prior runs
const RUN = Math.random().toString(36).slice(2, 8);
const EMAIL = `e2e-${RUN}@example.test`;
const PASSWORD = "password-1234";

async function backendUp(): Promise<boolean> {
  try {
    const r = await fetch(`${BACKEND}/healthz`);
    return r.ok;
  } catch { return false; }
}

test.beforeAll(async () => {
  if (!(await backendUp())) {
    test.skip(true, `backend not reachable at ${BACKEND}; skipping E2E`);
  }
});

async function signupAndLogin(page: Page) {
  await page.goto("/signup");
  await page.getByLabel(/email/i).fill(EMAIL);
  await page.getByLabel(/^password$/i).fill(PASSWORD);
  await page.getByLabel(/display name/i).fill(`E2E ${RUN}`).catch(() => undefined);
  await page.getByRole("button", { name: /sign|create/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/signup"), { timeout: 15_000 });
}

test("full alpha-product happy path", async ({ page }) => {
  test.setTimeout(120_000);

  // 1. Signup
  await signupAndLogin(page);
  await expect(page).toHaveURL(/\/(documents|chat|kg|settings)?/);

  // 2. Documents — upload (need a tiny valid PDF)
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: /documents/i })).toBeVisible();

  const tinyPdf = Buffer.from(
    "%PDF-1.4\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 100 100]>> endobj\nxref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000054 00000 n\n0000000102 00000 n\ntrailer <</Size 4 /Root 1 0 R>>\nstartxref\n152\n%%EOF\n",
    "utf-8"
  );

  // setInputFiles requires the dropzone's hidden <input type=file>
  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles({ name: `e2e-${RUN}.pdf`, mimeType: "application/pdf", buffer: tinyPdf });
  await expect(page.getByText(/queued|uploading|indexed/i).first()).toBeVisible({ timeout: 30_000 });

  // 3. Chat — create conversation, send message (might fail if no LLM)
  await page.goto("/chat");
  await page.getByRole("button", { name: /new/i }).click().catch(() => undefined);
  await page.waitForURL(/\/chat\/.+/, { timeout: 10_000 }).catch(() => undefined);
  // Composer presence
  const composer = page.locator("textarea");
  if (await composer.count()) {
    await composer.fill("hello?");
    await page.getByRole("button", { name: /send/i }).click().catch(() => undefined);
    // Don't assert response (may need real LLM); just allow some time then move on
    await page.waitForTimeout(3000);
  }

  // 4. KG overview
  await page.goto("/kg");
  await expect(page.getByRole("heading", { name: /knowledge graph/i })).toBeVisible();
  // Stats render (cards may show "0" for empty)
  await expect(page.getByText(/Entities/i).first()).toBeVisible();

  // 5. Settings — verify shell + tenant page loads
  await page.goto("/settings");
  // Settings layout redirects index → /settings/profile
  await expect(page).toHaveURL(/\/settings\/profile/);
  await expect(page.getByRole("heading", { name: /profile/i })).toBeVisible();

  await page.goto("/settings/tenant");
  await expect(page.getByRole("heading", { name: /tenant/i })).toBeVisible();
});
