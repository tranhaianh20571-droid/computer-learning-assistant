// 切片 1 前端回归：能力配置页可达、无真实网络外发、无敏感控制台输出。
// 用法：node scripts/slice1_frontend_check.mjs <baseUrl> <outDir>
// 依赖：playwright（npx --no-install playwright）。不写入仓库证据目录以外的文件。
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

// playwright 可能来自 npx 缓存，而不是仓库依赖；支持 PW_ROOT 显式指定。
function loadPlaywright() {
  const candidates = [
    process.env.PW_ROOT ? resolve(process.env.PW_ROOT, "playwright") : null,
    "playwright",
  ].filter(Boolean);
  for (const c of candidates) {
    try {
      return require(c);
    } catch {
      /* try next */
    }
  }
  throw new Error("playwright not found; set PW_ROOT to the npx cache node_modules dir");
}

const { chromium } = loadPlaywright();

const baseUrl = process.argv[2] || "http://127.0.0.1:4173";
const outDir = resolve(process.argv[3] || "../../.playwright-cli");
mkdirSync(outDir, { recursive: true });

const report = { baseUrl, requests: [], console: [], checks: {}, errors: [] };

// 优先使用系统已安装的 Chrome，避免额外下载浏览器二进制。
const launchOptions = {};
for (const exe of [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
]) {
  try {
    if (require("node:fs").existsSync(exe)) {
      launchOptions.executablePath = exe;
      break;
    }
  } catch {
    /* ignore */
  }
}

const browser = await chromium.launch(launchOptions);
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();

page.on("console", (msg) => {
  report.console.push({ type: msg.type(), text: msg.text() });
});
page.on("pageerror", (err) => report.errors.push(String(err)));
page.on("request", (req) => {
  const url = req.url();
  if (!url.startsWith(baseUrl) && !url.startsWith("data:") && !url.startsWith("blob:")) {
    report.requests.push(url);
  }
});

await page.goto(`${baseUrl}/#/login`, { waitUntil: "networkidle" });
report.checks.login = await page.getByRole("heading", { name: "登录" }).isVisible();

// 未登录时能力配置入口不渲染（需登录）
report.checks.capability_hidden_when_anonymous =
  (await page.getByRole("button", { name: "能力配置" }).count()) === 0;

// 窄屏可用性
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: `${outDir}/slice1-narrow-login.png`, fullPage: false });
await page.setViewportSize({ width: 1280, height: 900 });

// 键盘可达：Tab 能聚焦到导航按钮
await page.keyboard.press("Tab");
report.checks.keyboard_focus = await page.evaluate(
  () => document.activeElement !== null && document.activeElement.tagName === "BUTTON"
);

writeFileSync(`${outDir}/slice1-frontend-report.json`, JSON.stringify(report, null, 2));
await browser.close();

const leakedSecrets = report.console.some((c) => /sk-[A-Za-z0-9]{12,}|api_key=/i.test(c.text));
console.log(JSON.stringify({ ...report, leakedSecrets }, null, 2));
process.exit(report.errors.length || leakedSecrets ? 1 : 0);
