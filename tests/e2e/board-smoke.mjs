import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import path from "node:path";
import { execFileSync, spawn } from "node:child_process";
import { chromium } from "playwright";

const sourceRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const runRoot = mkdtempSync(path.join(tmpdir(), "jobboard-e2e-"));
cpSync(sourceRoot, runRoot, {
  recursive: true,
  filter: (source) => {
    const relative = path.relative(sourceRoot, source);
    if (!relative) return true;
    if ([".git", ".venv", "node_modules", ".playwright-cli", "artifacts"].some((name) =>
      relative === name || relative.startsWith(`${name}${path.sep}`))) return false;
    if (relative === "config/search.local.json") return false;
    if (relative === "data" || relative === "data/jobs.sample.json") return true;
    if (relative.startsWith(`data${path.sep}`)) return false;
    return true;
  },
});
symlinkSync(path.join(sourceRoot, ".venv"), path.join(runRoot, ".venv"), "dir");
const port = Number(process.env.JOBBOARD_E2E_PORT || 8897);
const baseURL = `http://127.0.0.1:${port}`;
const artifactDir = process.env.JOBBOARD_E2E_ARTIFACT_DIR || "/tmp/jobboard-e2e";
mkdirSync(artifactDir, { recursive: true });

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`${baseURL}/`);
      if (response.ok) return;
    } catch {
      // The local server is still starting.
    }
    await wait(100);
  }
  throw new Error(`Job Hunt Board did not start at ${baseURL}`);
}

execFileSync("./jobs", ["setup"], { cwd: runRoot, stdio: "pipe" });
const server = spawn("./.venv/bin/python", ["jobs.py", "serve", "--port", String(port), "--no-open"], {
  cwd: runRoot,
  stdio: "ignore",
});

const stopServer = () => {
  if (!server.killed) server.kill("SIGTERM");
};
process.once("exit", stopServer);
process.once("SIGINT", () => { stopServer(); process.exit(130); });
process.once("SIGTERM", () => { stopServer(); process.exit(143); });

try {
  await waitForServer();
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto(baseURL, { waitUntil: "networkidle" });
  assert.equal(await page.locator("h1.title").textContent(), "Your Job Hunt Board");
  assert.match(await page.locator("#status").textContent(), /Demo data/);
  const emptyDetailLabel = await page.locator("#detail").getAttribute("aria-labelledby");
  assert.equal(await page.locator(`#${emptyDetailLabel}`).count(), 1, "empty detail dialog must have a real label target");

  await page.locator("#q").fill("Product");
  assert.equal(await page.locator("#list .row").count(), 1, "sample search should find Product Designer");
  await page.locator("#list .row").first().click();
  assert.equal(await page.locator(".sample-apply").count(), 1, "demo detail must explain that Apply is disabled");
  assert.equal(await page.locator("a.apply").count(), 0, "demo data must not expose a fake Apply link");
  await page.screenshot({ path: path.join(artifactDir, "desktop-sample.png"), fullPage: true });

  // Swap the fixture for one synthetic live posting. This keeps the public
  // demo safe while proving the real Apply/state/search path without a network
  // call or a person's data.
  const samplePayload = JSON.parse(readFileSync(path.join(runRoot, "data", "jobs.sample.json"), "utf8"));
  const live = {
    ...samplePayload.opportunities[0],
    id: "greenhouse:acme:1234567",
    company: "Acme",
    company_slug: "acme",
    job_id: "1234567",
    title: "Analyst",
    department: "Finance",
    url: "https://boards.greenhouse.io/acme/jobs/1234567",
    read: false,
    dismissed: false,
  };
  writeFileSync(path.join(runRoot, "data", "jobs.local.json"), JSON.stringify({
    version: 1,
    generated_at: "2026-08-01T00:00:00Z",
    meta: { sample: false },
    opportunities: [live],
  }, null, 2));
  execFileSync("./jobs", ["board"], { cwd: runRoot, stdio: "pipe" });
  await page.reload({ waitUntil: "networkidle" });
  assert.doesNotMatch(await page.locator("#status").textContent(), /Demo data/);
  await page.locator("#q").fill("Finance");
  assert.equal(await page.locator("#list .row").count(), 1, "department must be searchable");
  await page.locator("#list .row").first().click();
  assert.equal(await page.locator("a.apply").count(), 1, "live fixture must expose the exact Apply link");
  assert.equal(await page.locator('[data-act="applied"]').textContent(), "Mark applied");

  // Existing installs used one global key before profile-scoped state shipped.
  // A first load must migrate that triage once, without leaking it to another
  // profile or overwriting a scoped object that already exists.
  await page.evaluate(() => {
    const row = document.querySelector("#list .row");
    const id = row?.getAttribute("data-id");
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("job-hunt-board-state-v2:")) localStorage.removeItem(key);
    }
    localStorage.removeItem("job-hunt-board-state-v2:legacy-migrated");
    localStorage.setItem("finance-job-board-state-v1", JSON.stringify({
      [id]: { applied: "2026-08-01", read: true },
    }));
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#q").fill("Finance");
  await page.locator("#list .row").first().click();
  assert.equal(await page.locator('[data-act="applied"]').textContent(), "✓ Applied",
    "legacy applied state should migrate into the profile-scoped key");
  assert.equal(await page.locator('[data-act="read"]').textContent(), "Read ✓",
    "legacy read state should migrate into the profile-scoped key");
  const migrated = await page.evaluate(() => Object.keys(localStorage).some((key) => key.startsWith("job-hunt-board-state-v2:") && key !== "job-hunt-board-state-v2:legacy-migrated"));
  assert.equal(migrated, true, "legacy triage must be copied to the new scoped key");
  await page.locator('[data-act="applied"]').click();
  assert.equal(await page.locator('[data-act="applied"]').textContent(), "Mark applied");
  await page.locator('[data-act="applied"]').click();
  assert.equal(await page.locator('[data-act="applied"]').textContent(), "✓ Applied");

  // A second search profile on the same origin must not inherit A's applied
  // overlay. Route one reload to an equivalent board with a different key.
  const boardHtml = readFileSync(path.join(runRoot, "artifacts", "board", "index.html"), "utf8");
  const otherProfileHtml = boardHtml.replace(/"search_profile_key"\s*:\s*"[^"]+"/, '"search_profile_key":"p-profile-b"');
  await page.route(`${baseURL}/`, async (route) => {
    await route.fulfill({ status: 200, contentType: "text/html", body: otherProfileHtml });
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#q").fill("Finance");
  await page.locator("#list .row").first().click();
  assert.equal(await page.locator('[data-act="applied"]').textContent(), "Mark applied",
    "a different search profile must start with clean browser triage state");
  await page.unroute(`${baseURL}/`);

  const unknownDismiss = await page.request.post(`${baseURL}/api/dismiss`, {
    data: { id: "missing-from-board", dismissed: true },
  });
  assert.equal(unknownDismiss.status(), 404, "unknown dismiss must not create a tombstone");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload({ waitUntil: "networkidle" });
  await page.locator("#q").fill("Product");
  await page.locator("#list .row").first().click();
  assert.equal(await page.locator("body").evaluate((body) => body.classList.contains("detail-open")), true);
  assert.equal(await page.locator("#detail").getAttribute("aria-modal"), "true");
  assert.equal(await page.evaluate(() => document.activeElement?.id), "detail-back");
  for (let i = 0; i < 5; i += 1) {
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#detail").evaluate((detail) => detail.contains(document.activeElement)), true,
      "mobile detail focus must remain trapped");
  }
  await page.screenshot({ path: path.join(artifactDir, "mobile-detail.png"), fullPage: true });

  // While a refresh is active, recovery actions stay hidden so a user cannot
  // accidentally cancel the in-flight job or start a duplicate run.
  await page.locator("#detail-back").click();
  await page.route("**/api/refresh**", async (route) => {
    if (route.request().url().endsWith("/api/refresh")) {
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ status: "started" }) });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ running: true, phase: "discovering", done: 2, total: 100, message: "Company 2…" }),
    });
  });
  await page.locator("#refresh-btn").click();
  await page.locator("#rf-go").click();
  await page.locator("#rf-loading").waitFor({ state: "visible" });
  assert.equal(await page.locator("#rf-erractions").isHidden(), true, "active refresh actions must stay hidden");
  assert.equal(await page.locator("#rf-erractions").evaluate((el) => getComputedStyle(el).display), "none");
  await page.screenshot({ path: path.join(artifactDir, "refresh-loading.png"), fullPage: true });
  await page.unroute("**/api/refresh**");
  await page.reload({ waitUntil: "networkidle" });

  // A stale/static server returns 404 for the local API. The refresh dialog
  // must explain the setup fix immediately, rather than spinning for 10s.
  await page.route("**/api/refresh**", async (route) => {
    await route.fulfill({ status: 404, contentType: "text/plain", body: "not a local app" });
  });
  await page.locator("#refresh-btn").click();
  await page.locator("#rf-go").click();
  await page.locator("#rf-noserver").waitFor({ state: "visible", timeout: 1000 });
  assert.match(await page.locator("#rf-noserver").textContent(), /\.\/jobs start/);
  await page.screenshot({ path: path.join(artifactDir, "refresh-no-server.png"), fullPage: true });

  await browser.close();
  assert.deepEqual(pageErrors, [], `browser page errors: ${pageErrors.join("; ")}`);
  console.log(`E2E smoke passed; screenshots: ${artifactDir}`);
} finally {
  stopServer();
  rmSync(runRoot, { recursive: true, force: true });
}
