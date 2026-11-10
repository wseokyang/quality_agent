"use strict";

// This test starts its own API and never connects to an existing deployment.
// Only synthetic content is used. Secrets, UI tickets and Authorization headers
// stay in process memory and are excluded from test output.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require("playwright");

const project = path.resolve(__dirname, "../..");
const python = process.env.QUALITY_PYTHON || path.join(
  project, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const work = fs.mkdtempSync(path.join(os.tmpdir(), "quality-browser-work-"));
const output = process.env.QUALITY_BROWSER_OUTPUT_DIR
  ? path.resolve(process.env.QUALITY_BROWSER_OUTPUT_DIR)
  : fs.mkdtempSync(path.join(os.tmpdir(), "quality-browser-results-"));
fs.mkdirSync(output, { recursive: true });

// Do not inherit production QUALITY_* settings, registry paths or signing keys.
const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("QUALITY_")));
Object.assign(env, {
  PYTHONPATH: path.join(project, "api"),
  PYTHONUNBUFFERED: "1",
  QUALITY_ENVIRONMENT: "development",
  QUALITY_ALLOW_SQLITE: "true",
  QUALITY_DATABASE_URL: `sqlite:///${path.join(work, "quality.db").replaceAll("\\", "/")}`,
  QUALITY_ARTIFACT_DIR: path.join(work, "artifacts"),
  QUALITY_RECOVERY_EPOCH: crypto.randomUUID(),
  QUALITY_USER_TEAMS_JSON: JSON.stringify({ "browser-test-user": ["quality-test-team"] }),
  QUALITY_CURATOR_IDS_JSON: JSON.stringify(["browser-test-user"]),
  QUALITY_TOOL_REGISTRY_JSON: "[]",
});
const secrets = [];
for (const role of ["HUMAN", "MODEL", "TRACE", "WORKER"]) {
  const secret = crypto.randomBytes(48).toString("base64url");
  secrets.push(secret);
  env[`QUALITY_${role}_JWT_SECRET`] = secret;
}

function redact(value) {
  let result = String(value);
  for (const secret of secrets) result = result.replaceAll(secret, "[redacted]");
  return result.replace(/#ticket=[^\s"')]+/g, "#ticket=[redacted]")
    .replace(/Bearer\s+[^\s"']+/gi, "Bearer [redacted]")
    .replace(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, "[redacted JWT]");
}

function cli(args) {
  const result = spawnSync(python, ["-m", "quality_api.cli", ...args], {
    cwd: project, env, encoding: "utf8", timeout: 30_000, windowsHide: true,
  });
  if (result.error || result.status !== 0) {
    throw new Error(redact(`Quality CLI ${args[0]} failed: ${result.error?.message || result.stderr || result.stdout}`));
  }
  return result.stdout.trim();
}

async function freePort() {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolve);
  });
  const port = listener.address().port;
  await new Promise((resolve, reject) => listener.close((error) => error ? reject(error) : resolve()));
  return port;
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
let server;
let browser;
let page;
let serverLog = "";
let interrupted = false;

async function stopServer() {
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  const exited = new Promise((resolve) => server.once("exit", resolve));
  server.kill("SIGTERM");
  let timeout;
  const stopped = await Promise.race([
    exited.then(() => true), new Promise((resolve) => { timeout = setTimeout(() => resolve(false), 5_000); }),
  ]);
  clearTimeout(timeout);
  if (!stopped) { server.kill("SIGKILL"); await exited; }
}

async function ready(origin) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (interrupted) throw new Error("Test interrupted.");
    if (server.exitCode !== null || server.signalCode !== null) {
      throw new Error(`Temporary API exited before it was ready. ${redact(serverLog)}`);
    }
    try {
      const response = await fetch(`${origin}/health/ready`, { signal: AbortSignal.timeout(1_000) });
      if (response.ok && (await response.json()).status === "ready") return;
    } catch (_) { /* The API can still be starting. */ }
    await sleep(150);
  }
  throw new Error(`Temporary API did not become ready. ${redact(serverLog)}`);
}

async function submit(dialog, label) {
  await dialog.getByRole("button", { name: label, exact: true }).click();
  await dialog.waitFor({ state: "hidden" });
}

async function download(button, filename) {
  const waiting = page.waitForEvent("download");
  await button.click();
  const item = await waiting;
  const target = path.join(output, filename);
  await item.saveAs(target);
  assert.equal(await item.failure(), null);
  return target;
}

async function flow(origin) {
  const ticketUrl = cli([
    "create-demo-ticket", "--user", "browser-test-user", "--chat", `browser-${crypto.randomUUID()}`, "--url", origin,
  ]);
  assert.ok(ticketUrl.startsWith(`${origin}/ui/index.html#ticket=`), "CLI must issue a local one-use UI ticket");
  const launch = { headless: true };
  if (process.env.QUALITY_BROWSER_EXECUTABLE) launch.executablePath = path.resolve(process.env.QUALITY_BROWSER_EXECUTABLE);
  browser = await chromium.launch(launch);
  if (interrupted) throw new Error("Test interrupted.");
  page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
  page.setDefaultTimeout(15_000);
  const errors = [];
  const cspErrors = [];
  const requests = [];
  page.on("pageerror", (error) => errors.push(redact(error.message)));
  page.on("console", (message) => {
    if (/Content Security Policy|Refused to (?:execute|apply|load)/i.test(message.text())) cspErrors.push(redact(message.text()));
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith("/quality/v1/")) requests.push({ path: url.pathname, status: response.status() });
  });
  // No route interception: all API traffic and downloads use the actual server.
  await page.goto(ticketUrl);
  await page.getByRole("button", { name: "새 업무", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("업무 제목", { exact: true }).fill("브라우저 실연동 품질 검토");
  await dialog.getByLabel("업무 목적", { exact: true }).fill("원문 등록부터 보고서, 지식 저장까지 확인");
  await submit(dialog, "업무 만들기");
  assert.equal(new URL(page.url()).hash, "", "The exchanged ticket must be removed from the URL");

  await page.getByRole("button", { name: "원천 자료", exact: true }).click();
  await page.getByRole("button", { name: "자료 직접 등록", exact: true }).click();
  await dialog.getByLabel("자료 이름", { exact: true }).fill("고객 접수 집계");
  const sourceText = '최근 30일 동일 유형 접수는 3건입니다. <img src=x onerror="window.XSS=1">';
  await dialog.getByLabel("원문", { exact: true }).fill(sourceText);
  await submit(dialog, "자료 등록");
  await page.getByText("원문 확인", { exact: true }).click();
  assert.ok((await page.locator("#panel").textContent()).includes(sourceText));
  assert.equal(await page.locator('img[src="x"]').count(), 0, "Untrusted source text must not become HTML");
  assert.equal(await page.evaluate(() => window.XSS), undefined);

  await page.getByRole("button", { name: "근거 검토", exact: true }).click();
  await page.getByRole("button", { name: "근거 직접 작성", exact: true }).click();
  await dialog.getByLabel("근거 요약", { exact: true }).fill("최근 30일 동일 유형 접수 3건");
  await dialog.getByLabel("해석", { exact: true }).fill("제품군별 추가 확인이 필요합니다.");
  await dialog.getByRole("checkbox").check();
  await submit(dialog, "근거 제안 저장");
  await page.getByRole("button", { name: "채택", exact: true }).click();
  await submit(dialog, "채택");

  await page.getByRole("button", { name: "결과 · 보고서", exact: true }).click();
  await page.getByRole("button", { name: "결과 확정", exact: true }).click();
  await dialog.getByLabel("결과 설명", { exact: true }).fill("동일 유형 3건에 대해 제품군 기준으로 추가 조사합니다.");
  await submit(dialog, "결과 확정");

  for (const [buttonName, headingName, filename] of [
    ["Markdown 보고서", "MD 보고서", "report.md"],
    ["Word 보고서", "DOCX 보고서", "report.docx"],
  ]) {
    await page.getByRole("button", { name: buttonName, exact: true }).click();
    await page.getByRole("heading", { name: headingName, exact: true }).waitFor();
    cli(["worker", "--once"]);
    await page.getByRole("button", { name: "새로고침", exact: true }).click();
    const report = page.locator(".card").filter({ has: page.getByRole("heading", { name: headingName, exact: true }) });
    const file = await download(report.getByRole("button", { name: "다운로드", exact: true }), filename);
    if (filename.endsWith(".md")) assert.match(fs.readFileSync(file, "utf8"), /최근 30일/);
    else assert.equal(fs.readFileSync(file).subarray(0, 2).toString(), "PK", "DOCX must be an OOXML ZIP");
  }

  await page.getByRole("button", { name: "이 결과로 업무 완료", exact: true }).click();
  await submit(dialog, "업무 완료");
  await page.getByRole("button", { name: "절차 · 지식", exact: true }).click();
  await page.getByRole("button", { name: "지식 초안 작성", exact: true }).click();
  await dialog.getByLabel("제목", { exact: true }).fill("품질 접수 비교 확인 사항");
  await dialog.getByLabel("요약", { exact: true }).fill("같은 제품군과 기간으로 비교");
  await dialog.getByLabel("내용", { exact: true }).fill("고객 접수 비교 시 기간을 고정합니다.");
  await dialog.getByLabel("적용 조건 · 범위", { exact: true }).fill("동일 유형 품질 접수");
  await dialog.getByRole("checkbox").check();
  await submit(dialog, "초안 저장");
  await page.getByRole("button", { name: "개인 저장 승인", exact: true }).click();
  await submit(dialog, "개인 저장 승인");
  const exported = await download(page.getByRole("button", { name: "내보내기", exact: true }), "knowledge.zip");
  assert.equal(fs.readFileSync(exported).subarray(0, 2).toString(), "PK");

  // User-provided sources cannot silently become team-shareable knowledge.
  await page.getByRole("button", { name: "팀 공유 요청", exact: true }).click();
  const denied = page.waitForResponse((response) => response.url().endsWith("/submit") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "공유 요청", exact: true }).click();
  const response = await denied;
  assert.equal(response.status(), 403);
  assert.equal((await response.json()).error.code, "SOURCE_NOT_SHAREABLE");
  await dialog.locator(".form-error").waitFor();
  await dialog.getByRole("button", { name: "취소", exact: true }).click();

  await page.getByRole("button", { name: "업무 다시 열기", exact: true }).click();
  await dialog.getByLabel("사유", { exact: true }).fill("추가 자료 반영");
  await submit(dialog, "업무 다시 열기");
  await page.getByRole("button", { name: "근거 검토", exact: true }).click();
  await page.getByRole("button", { name: "내용 수정", exact: true }).click();
  await dialog.getByLabel("근거 요약", { exact: true }).fill("최근 30일 동일 유형 접수 3건 (검토 보완)");
  await submit(dialog, "새 버전 저장");
  await page.getByText("검토 전", { exact: true }).waitFor();
  await page.getByRole("button", { name: "결과 · 보고서", exact: true }).click();
  await page.getByText("재확인 필요", { exact: true }).nth(2).waitFor();
  assert.equal(await page.getByText("재확인 필요", { exact: true }).count(), 3, "Snapshot and both reports must require review");

  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  assert.equal(await page.evaluate(() => window.XSS), undefined);
  assert.deepEqual(errors, [], "Browser JavaScript must not throw");
  assert.deepEqual(cspErrors, [], "The UI must work under the API's actual CSP");
  const denials = requests.filter((request) => request.status >= 400);
  assert.equal(denials.length, 1, "Only the deliberate nonshareable-source request may fail");
  assert.equal(denials[0].status, 403);
  await page.screenshot({ path: path.join(output, "desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "Mobile layout must not overflow horizontally");
  await page.screenshot({ path: path.join(output, "mobile.png"), fullPage: true });
  return {
    status: "PASS", realApiRequests: requests.length, expectedDenials: denials,
    checks: [
      "case creation with registered profile", "user-text source and XSS-safe rendering",
      "manual evidence proposal and adoption", "immutable result snapshot",
      "Markdown and DOCX worker generation and authenticated download", "case completion and reopening",
      "knowledge draft, approval and ZIP export", "nonshareable source rejected for team sharing",
      "new evidence version invalidates the snapshot and reports", "actual CSP and browser storage checks",
      "desktop and mobile layout",
    ],
  };
}

async function checkReverseProxyPrefix(origin) {
  // A separate, explicitly mocked page checks URL construction beneath a proxy
  // prefix. The full workflow above continues to use the real unmocked API.
  const prefixed = await browser.newPage();
  const calls = [];
  const unexpected = [];
  await prefixed.route("**/*", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/service/ui/")) {
      const filename = pathname.slice("/service/ui/".length);
      if (["index.html", "app.js", "styles.css"].includes(filename)) {
        return route.fulfill({
          body: fs.readFileSync(path.join(project, "api/quality_api/static", filename)),
          contentType: filename.endsWith(".js") ? "application/javascript" : filename.endsWith(".css") ? "text/css" : "text/html",
        });
      }
    }
    calls.push(pathname);
    if (pathname === "/service/quality/v1/ui/exchange") {
      assert.deepEqual(request.postDataJSON(), { ticket: "prefix-smoke" });
      return route.fulfill({ json: { data: {
        access_token: "prefix-test-token", expires_in: 300, context: { chat_id: "prefix-chat" },
      } } });
    }
    if (pathname === "/service/quality/v1/profiles" || pathname === "/service/quality/v1/cases") {
      assert.equal(request.headers().authorization, "Bearer prefix-test-token");
      return route.fulfill({ json: { data: { items: pathname.endsWith("/profiles")
        ? [{ profile_id: "quality-general", profile_version: 1 }] : [] } } });
    }
    unexpected.push(pathname);
    return route.fulfill({ status: 404, json: { error: { message: "Unexpected prefixed request" } } });
  });
  try {
    await prefixed.goto(`${origin}/service/ui/index.html#ticket=prefix-smoke`);
    await prefixed.getByText("아직 연결된 업무가 없습니다.", { exact: true }).waitFor();
    assert.deepEqual(calls, [
      "/service/quality/v1/ui/exchange", "/service/quality/v1/profiles", "/service/quality/v1/cases",
    ]);
    assert.deepEqual(unexpected, []);
    assert.equal(new URL(prefixed.url()).hash, "");
  } finally { await prefixed.close(); }
}

async function main() {
  try {
    if (!fs.existsSync(python) && python.includes(path.sep)) {
      throw new Error("Python environment not found. Create quality_agent/.venv or set QUALITY_PYTHON; see tests/browser/README.md.");
    }
    cli(["migrate"]);
    const port = await freePort();
    const origin = `http://127.0.0.1:${port}`;
    server = spawn(python, [
      "-m", "uvicorn", "quality_api.main:create_app", "--factory", "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning",
    ], { cwd: project, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    const recordLog = (chunk) => { serverLog = (serverLog + redact(chunk)).slice(-16_000); };
    server.stdout.on("data", recordLog); server.stderr.on("data", recordLog);
    server.on("error", (error) => recordLog(error.message));
    await ready(origin);
    const result = await flow(origin);
    await checkReverseProxyPrefix(origin);
    result.checks.push("reverse-proxy /service prefix (separate mocked URL check)");
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify(result, null, 2) + "\n");
    console.log(JSON.stringify({ ...result, outputDirectory: output }, null, 2));
  } catch (error) {
    if (page && !page.isClosed()) {
      await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    }
    fs.writeFileSync(path.join(output, "failure.txt"), redact(error.stack || error.message) + "\n");
    throw error;
  } finally {
    if (browser) await browser.close().catch(() => {});
    await stopServer();
    if (serverLog) fs.writeFileSync(path.join(output, "api.log"), redact(serverLog));
    // This directory contains the temporary database and internal artifact store.
    // Only downloaded reports and screenshots in the separate output directory remain.
    fs.rmSync(work, { recursive: true, force: true });
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    interrupted = true;
    if (server) server.kill("SIGTERM");
    if (browser) void browser.close().catch(() => {});
  });
}

main().catch((error) => {
  console.error(redact(error.stack || error.message));
  console.error(`Test output: ${output}`);
  process.exitCode = 1;
});
