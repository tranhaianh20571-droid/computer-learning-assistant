const fs = require("node:fs/promises");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const execute = promisify(execFile);

async function protectHome(home) {
  if (process.platform !== "win32") throw new Error("PLATFORM_UNSUPPORTED");
  const system = path.join(process.env.SystemRoot, "System32");
  const { stdout } = await execute(path.join(system, "WindowsPowerShell/v1.0/powershell.exe"),
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
      "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value | ConvertTo-Json -Compress"],
    { windowsHide: true, timeout: 15000 });
  const sid = JSON.parse(stdout.trim());
  if (!/^S-1-[0-9]+(?:-[0-9]+){1,15}$/.test(sid)) throw new Error("DATA_DIRECTORY_LOCKED");
  await fs.mkdir(home, { recursive: true });
  const marker = path.join(home, ".windows-owner");
  try {
    if ((await fs.readFile(marker, "utf8")) !== sid) throw new Error("DATA_DIRECTORY_LOCKED");
    return;
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  await execute(path.join(system, "icacls.exe"),
    [home, "/inheritance:r", "/grant:r", `*${sid}:(OI)(CI)F`, "*S-1-5-18:(OI)(CI)F", "/Q"],
    { windowsHide: true, timeout: 30000 });
  await fs.writeFile(marker, sid, { flag: "wx" });
}

module.exports = { protectHome };
