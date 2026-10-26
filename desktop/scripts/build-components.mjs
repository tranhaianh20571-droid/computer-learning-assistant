import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";

const require = createRequire(import.meta.url);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const build = path.join(root, ".backup/desktop-build");
const output = path.join(root, ".backup/desktop-components");
const uv = process.env.UV_EXE || "uv";
const seven = require("7zip-bin").path7za;
const version = JSON.parse(await fs.readFile(path.join(root, "desktop/package.json"))).version;
const base = `https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/v${version}/`;
const components = [];
await fs.mkdir(build, { recursive: true });
await fs.mkdir(output, { recursive: true });

async function run(command, args, options = {}) {
  await new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: root, windowsHide: true, stdio: "inherit", ...options });
    child.on("error", reject);
    child.on("exit", code => code === 0 ? resolve() : reject(new Error(`${path.basename(command)} failed (${code})`)));
  });
}
async function hash(file) {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  return digest.digest("hex");
}
async function acquire(url, expected, file) {
  if (await hash(file).catch(() => "") === expected) return file;
  const response = await fetch(url, { signal: AbortSignal.timeout(600000) });
  if (!response.ok) throw new Error(`Download failed (${response.status})`);
  await pipeline(response.body, createWriteStream(file + ".part"));
  if (await hash(file + ".part") !== expected) throw new Error("Upstream checksum mismatch");
  await fs.rename(file + ".part", file);
  return file;
}
async function size(directory) {
  let bytes = 0;
  for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    bytes += entry.isDirectory() ? await size(file) : (await fs.stat(file)).size;
  }
  return bytes;
}
async function archive(id, directory) {
  const file = `${id}-${version}-win32-x64.zip`;
  const destination = path.join(output, file);
  await fs.rm(destination, { force: true });
  await run(seven, ["a", "-tzip", "-mx=3", destination, ".", "-xr!.sandbox*", "-xr!.preflight*"],
    { cwd: directory, stdio: "ignore" });
  const bytes = (await fs.stat(destination)).size;
  components.push({ id, version, url: base + file, bytes, unpackedBytes: Math.max(bytes, await size(directory)),
    sha256: await hash(destination) });
}
async function python(name, pythonVersion, requirements, extraArgs = []) {
  const lock = path.join(root, "desktop/locks", name + ".txt");
  if (await fs.stat(lock).catch(() => null)) requirements = lock;
  const directory = path.join(build, name);
  if (!(await fs.stat(path.join(directory, "python.exe")).catch(() => null))) {
    const install = path.join(build, "interpreters");
    await run(uv, ["python", "install", pythonVersion, "--install-dir", install, "--no-bin"]);
    const match = (await fs.readdir(install)).find(item => item.startsWith(`cpython-${pythonVersion}-windows-x86_64-none`));
    if (!match) throw new Error("Pinned interpreter unavailable");
    await fs.cp(path.join(install, match), directory, { recursive: true });
  }
  if (!(await fs.stat(path.join(directory, ".dependencies-ready")).catch(() => null))) {
    await run(uv, ["pip", "install", "--compile-bytecode", "--break-system-packages", "--python", path.join(directory, "python.exe"),
      "--requirements", requirements, ...extraArgs]);
    await run(path.join(directory, "python.exe"), ["-m", "compileall", "-q", "-f",
      "--invalidation-mode=unchecked-hash", "-x", String.raw`[\\/]olefile2\.py$`,
      path.join(directory, "Lib")]);
    await fs.writeFile(path.join(directory, ".dependencies-ready"), version);
  }
  await runtimeLibraries(directory);
  return directory;
}

async function runtimeLibraries(directory) {
  const license = path.join(directory, "licenses/vclibs");
  await fs.mkdir(license, { recursive: true });
  for (const file of await fs.readdir(path.join(build, "vclibs"))) {
    if (!file.toLowerCase().endsWith(".dll")) continue;
    if (!await fs.stat(path.join(directory, file)).catch(() => null)) {
      await fs.copyFile(path.join(build, "vclibs", file), path.join(directory, file));
    }
  }
  await fs.copyFile(path.join(build, "vclibs/AppxManifest.xml"), path.join(license, "AppxManifest.xml"));
  await fs.writeFile(path.join(license, "source.json"), JSON.stringify({
    url: "https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx",
    sha256: "b56a9101f706f9d95f815f5b7fa6efbac972e86573d378b96a07cff5540c5961"
  }));
}
const vclibs = await acquire("https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx",
  "b56a9101f706f9d95f815f5b7fa6efbac972e86573d378b96a07cff5540c5961", path.join(build, "vclibs.appx"));
await run(seven, ["x", "-y", vclibs, "-o" + path.join(build, "vclibs")], { stdio: "ignore" });

console.log("Building core runtime");
const coreRequirements = path.join(build, "core-requirements.txt");
await run(uv, ["export", "--project", "server/backend", "--frozen", "--no-dev",
  "--no-emit-project", "--no-hashes", "--format", "requirements-txt", "--output-file", coreRequirements],
  { stdio: "ignore" });
const core = await python("core", "3.14.7", coreRequirements);
await archive("core", core);

console.log("Building portable PostgreSQL");
const pg0 = await acquire(
  "https://github.com/vectorize-io/pg0/releases/download/v0.15.2/pg0-windows-x86_64.exe",
  "8894575679886fef41e176608da16f4448fdeea4cc60c02380422adc147260b7",
  path.join(build, "pg0.exe"));
// pg0 embeds official PostgreSQL/pgvector binaries. This isolated build instance
// only unpacks them; no developer or product database is opened.
const pgName = "study-build-" + process.pid;
try {
  await run(pg0, ["start", "--name", pgName, "--data-dir", path.join(build, "pg-extract-data"),
    "--username", "build", "--password", "throwaway-build-only", "--database", "build", "--port", "0"],
    { stdio: "ignore" });
} finally {
  await run(pg0, ["stop", "--name", pgName], { stdio: "ignore" });
}
const postgres = path.join(build, "postgres");
await fs.cp(path.join(os.homedir(), ".pg0/installation/18.1.0"), postgres, { recursive: true });
await runtimeLibraries(path.join(postgres, "bin"));
await run(path.join(core,"python.exe"),["desktop/scripts/patch-postgres.py",postgres]);
await archive("postgres", postgres);

console.log("Building video runtime and default model");
const video = await python("video", "3.14.7", path.join(root, "server/source-worker/requirements.txt"));
await archive("video", video);
const asr = path.join(build, "asr-small");
await run(path.join(video, "python.exe"), [path.join(root, "server/source-worker/prepare_models.py"),
  "--model", "small", "--directory", asr]);
await archive("asr-small", asr);

console.log("Building personal memory runtime");
const memoryRequirements = path.join(root, "desktop/requirements-memory.txt");
const memory = await python("memory", "3.14.7", memoryRequirements);
await archive("memory", memory);

console.log("Building isolated CPU classroom runtime");
const classroomRequirements = path.join(root, "desktop/requirements-classroom.txt");
const classroom = await python("classroom", "3.12.12", classroomRequirements);
const jdkZip = await acquire(
  "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.17%2B10/OpenJDK17U-jdk_x64_windows_hotspot_17.0.17_10.zip",
  "dcf0064efec7e515a5e3b56e7532f1a1c125510303c6c9e60af8878e3f7347fe", path.join(build, "jdk.zip"));
await run(seven, ["x", "-y", jdkZip, "-o" + path.join(build, "jdk-unpack")], { stdio: "ignore" });
await fs.cp(path.join(build, "jdk-unpack/jdk-17.0.17+10"), path.join(classroom, "jdk"), { recursive: true });
const mingwArchive = await acquire(
  "https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/llvm-mingw-20261020-ucrt-x86_64.zip",
  "fd5462b7f5d93b2ff981fcd3f9c7e427e47f3203c64ea91c27bcc444eebd74cd", path.join(build, "llvm-mingw.zip"));
await run(seven, ["x", "-y", mingwArchive, "-o" + path.join(build, "llvm-unpack")], { stdio: "ignore" });
await fs.rm(path.join(classroom, "mingw"), { recursive: true, force: true });
await fs.cp(path.join(build, "llvm-unpack/llvm-mingw-20261020-ucrt-x86_64"), path.join(classroom, "mingw"), { recursive: true });
await run(uv, ["run", "--no-project", "--python", path.join(classroom, "python.exe"),
  "--with", "pefile==2024.8.26", "desktop/scripts/patch-llvm.py", classroom]);
await run("dotnet", ["publish", "desktop/sandbox/Sandbox.csproj", "-c", "Release", "-r", "win-x64",
  "--self-contained", "true", "-o", path.join(classroom, "sandbox")]);
await archive("classroom", classroom);

const manifest = { schema: 1, version, platform: "win32-x64", components };
await fs.mkdir(path.join(root, "desktop/manifests"), { recursive: true });
await fs.writeFile(path.join(root, "desktop/manifests/runtime.json"), JSON.stringify(manifest, null, 2) + "\n");
await fs.writeFile(path.join(output, "runtime.json"), JSON.stringify(manifest, null, 2) + "\n");
console.log("All default runtime components prepared.");
