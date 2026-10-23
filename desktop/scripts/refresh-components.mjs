// Repackage explicitly selected, already-built components after a local repair.
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { createReadStream } from "node:fs";
import { createHash } from "node:crypto";
const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const manifestPath = path.join(root, "desktop/manifests/runtime.json");
const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
const sandboxOnly = process.argv.includes("--sandbox-only");
const wanted = new Set(process.argv.slice(2).filter(value => value !== "--sandbox-only"));
if (sandboxOnly && (wanted.size !== 1 || !wanted.has("classroom"))) throw new Error("Sandbox update requires classroom only");
async function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    let output = "";
    const child = spawn(command, args, { cwd: root, windowsHide: true, ...options });
    child.stdout?.on("data", data => { output += data.toString(); });
    child.on("error", reject);
    child.on("exit", code => code === 0 ? resolve(output) : reject(new Error("Repackage failed")));
  });
}
async function size(directory) {
  let bytes = 0;
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.name.startsWith(".sandbox") || entry.name.startsWith(".preflight")) continue;
    bytes += entry.isDirectory() ? await size(file) : (await fs.stat(file)).size;
  }
  return bytes;
}
await fs.mkdir(path.join(root, "desktop/locks"), { recursive: true });
for (const item of manifest.components) {
  const directory = path.join(root, ".backup/desktop-build", item.id);
  if (wanted.has(item.id) && item.id !== "asr-small") {
    const target = item.id === "postgres" ? path.join(directory, "bin") : directory;
    for (const file of await fs.readdir(path.join(root, ".backup/desktop-build/vclibs"))) {
      if (file.toLowerCase().endsWith(".dll") && !await fs.stat(path.join(target, file)).catch(() => null)) {
        await fs.copyFile(path.join(root, ".backup/desktop-build/vclibs", file), path.join(target, file));
      }
    }
    const license = path.join(target, "licenses/vclibs");
    await fs.mkdir(license, { recursive: true });
    await fs.copyFile(path.join(root, ".backup/desktop-build/vclibs/AppxManifest.xml"), path.join(license, "AppxManifest.xml"));
  }
  if (["core", "video", "memory", "classroom"].includes(item.id)) {
    const requirements = await run(process.env.UV_EXE || "uv", ["pip", "freeze", "--python", path.join(directory, "python.exe")],
      { stdio: ["ignore", "pipe", "inherit"] });
    const header = item.id === "classroom" ? "--extra-index-url https://download.pytorch.org/whl/cpu\n" : "";
    const portable = requirements.split(/\r?\n/).filter(line => !/^pip(?:==| @ )/.test(line));
    if (portable.some(line => /\s@\sfile:/i.test(line))) throw new Error("Nonportable dependency reference");
    await fs.writeFile(path.join(root, "desktop/locks", item.id + ".txt"), header + portable.join("\n"));
  }
  if (!wanted.has(item.id)) continue;
  console.log("Repackaging " + item.id);
  const archive = path.join(root, ".backup/desktop-components", new URL(item.url).pathname.split("/").pop());
  if (!sandboxOnly) await fs.rm(archive, { force: true });
  await run(require("7zip-bin").path7za, sandboxOnly
    ? ["u", "-tzip", "-mx=3", archive, "sandbox/Sandbox.exe"]
    : ["a", "-tzip", "-mx=3", archive, ".", "-xr!.sandbox*", "-xr!.preflight*"],
    { cwd: directory, stdio: "ignore" });
  const hash = createHash("sha256");
  for await (const part of createReadStream(archive)) hash.update(part);
  item.sha256 = hash.digest("hex");
  item.bytes = (await fs.stat(archive)).size;
  item.unpackedBytes = Math.max(item.bytes, await size(directory));
}
await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
await fs.copyFile(manifestPath, path.join(root, ".backup/desktop-components/runtime.json"));
console.log("Runtime manifest and dependency locks updated.");
