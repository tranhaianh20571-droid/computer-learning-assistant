const fs = require("node:fs/promises");
const path = require("node:path");
const { execFile, spawn } = require("node:child_process");
const { promisify } = require("node:util");
const { externalUrl } = require("./external.cjs");
const execute = promisify(execFile);

async function executableFile(value) {
  if (typeof value !== "string" || !path.isAbsolute(value) || !/\.exe$/i.test(value)) return false;
  const file = await fs.open(value, "r").catch(() => null);
  if (!file) return false;
  try {
    const signature = Buffer.alloc(2);
    return (await file.stat()).isFile() && (await file.read(signature, 0, 2, 0)).bytesRead === 2 &&
      signature.toString("ascii") === "MZ";
  } finally { await file.close(); }
}

async function discoverBrowsers(script = path.join(__dirname, "detect-browsers.ps1")) {
  const powershell = path.join(process.env.SystemRoot, "System32/WindowsPowerShell/v1.0/powershell.exe");
  const { stdout } = await execute(powershell, ["-NoLogo", "-NoProfile", "-NonInteractive",
    "-ExecutionPolicy", "Bypass", "-File", script],
    { windowsHide: true, timeout: 15000, maxBuffer: 1024 * 1024 });
  const values = JSON.parse(stdout.trim().replace(/^\uFEFF/, ""));
  if (!Array.isArray(values) || values.length > 200) throw new Error("BROWSER_DISCOVERY_FAILED");
  const found = new Map();
  for (const item of values) {
    if (!item || typeof item.name !== "string" || typeof item.executable !== "string" ||
        path.basename(item.executable).toLowerCase() === "iexplore.exe" || !await executableFile(item.executable)) continue;
    const executable = path.resolve(item.executable);
    if (!found.has(executable.toLowerCase()))
      found.set(executable.toLowerCase(), { name: item.name.slice(0, 100), executable });
  }
  return [...found.values()].sort((a, b) => a.name.localeCompare(b.name));
}

async function launchBrowser(executable, value, launcher = spawn) {
  const url = externalUrl(value);
  if (!await executableFile(executable)) throw new Error("BROWSER_UNAVAILABLE");
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
    /^(?:SystemRoot|WINDIR|COMSPEC|USERPROFILE|APPDATA|LOCALAPPDATA|TEMP|TMP|ProgramFiles(?:\(x86\))?|ProgramData|ALLUSERSPROFILE|HOMEDRIVE|HOMEPATH|USERNAME|USERDOMAIN|PATH)$/i.test(name)));
  await new Promise((resolve, reject) => {
    const child = launcher(executable, [url], { shell: false, detached: true, windowsHide: false, stdio: "ignore", env });
    child.once("error", reject);
    child.once("spawn", () => { child.unref(); resolve(); });
  });
}

module.exports = { discoverBrowsers, executableFile, launchBrowser };
