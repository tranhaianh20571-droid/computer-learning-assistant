// Simulates a new installation using empty data/cache and a deliberately narrow
// process environment. It is not a substitute for a separate clean Windows VM.
import fs from "node:fs/promises";
import path from "node:path";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
const require = createRequire(import.meta.url);
const { validateManifest, prepare } = require("../desktop/src/install.cjs");
const { protectHome } = require("../desktop/src/paths.cjs");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const home = path.join(root, ".backup", "新电脑 模拟-" + Date.now());
const resources = path.join(root, ".backup/desktop-components");
await fs.mkdir(home, { recursive: true });
await protectHome(home);
const probe = process.env.STUDY_BOOT_PROBE === "1";
const manifest = probe ? null : validateManifest(JSON.parse(await fs.readFile(path.join(resources, "runtime.json"), "utf8")));
const server = http.createServer(async (req, res) => {
  try {
    const file = path.join(resources, path.basename(req.url));
    const { createReadStream } = await import("node:fs");
    const total = (await fs.stat(file)).size;
    const offset = Number(req.headers.range?.match(/^bytes=(\d+)-$/)?.[1] || 0);
    res.writeHead(offset ? 206 : 200, offset ? { "content-range": `bytes ${offset}-${total - 1}/${total}` } : {});
    createReadStream(file, { start: offset }).pipe(res);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
try {
  let last = "";
  const notify = value => {
    const current = value.component + ":" + value.stage;
    if (current !== last) { console.log(current); last = current; }
  };
  const roots = probe ? Object.fromEntries(["core", "postgres", "video", "asr-small", "memory", "classroom"]
    .map(name => [name, path.join(root, ".backup/desktop-build", name)])) :
    await prepare(manifest, home, notify, { fetcher: url =>
      fetch(`http://127.0.0.1:${server.address().port}/${new URL(url).pathname.split("/").pop()}`) });
  const keys = Object.fromEntries(["database", "memory", "proxy", "encryption", "session", "gateway"]
    .map(key => [key, randomBytes(32).toString("base64url")]));
  const config = { home, roots, keys, backend: path.join(root, "server/backend"),
    worker: path.join(root, "server/source-worker"), memoryScript: path.join(root, "deploy/memory_service.py") };
  await fs.mkdir(path.join(home, "cache/tmp"), { recursive: true });
  const env = { SystemRoot: process.env.SystemRoot, WINDIR: process.env.WINDIR,
    PATH: path.join(process.env.SystemRoot, "System32"), TEMP: path.join(home, "cache/tmp"),
    TMP: path.join(home, "cache/tmp"), PYTHONPATH: config.backend, PYTHONNOUSERSITE: "1", PYTHONUTF8: "1",
    PYTHONDONTWRITEBYTECODE: "1", USERPROFILE: home, LOCALAPPDATA: path.join(home, "local") };
  async function boot() {
    const child = spawn(path.join(roots.classroom, "sandbox/Sandbox.exe"), ["--supervise"],
      { cwd: home, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    child.stdin.write(JSON.stringify({ executable: path.join(roots.core, "python.exe"),
      args: ["-u", "-m", "app.desktop_runtime"], directory: home, owner: process.pid, payload: config }) + "\n");
    const ready = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Desktop boot timed out")), 600000);
      let text = "";
      child.stdout.on("data", chunk => {
        text += chunk.toString();
        let at;
        while ((at = text.indexOf("\n")) >= 0) {
          const line = text.slice(0, at); text = text.slice(at + 1);
          try {
            const value = JSON.parse(line);
            if (value.stage === "starting") console.log("service:" + value.component +
              (value.port ? ":" + value.port : "") + (value.probe_status !== undefined ? ":http=" + value.probe_status : ""));
            if (value.stage === "failed") { clearTimeout(timer); reject(new Error(JSON.stringify(value))); }
            if (value.stage === "ready") { clearTimeout(timer); resolve(value); }
          } catch (error) { if (error.message.startsWith("{")) reject(error); }
        }
      });
      child.once("exit", code => { clearTimeout(timer); reject(new Error("Supervisor exited: " + code)); });
    }).catch(async error => { child.stdin.end(); throw error; });
    return { child, ready };
  }
  async function shutdown(child) {
    child.stdin.end();
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Shutdown timed out")), 45000);
      child.once("exit", () => { clearTimeout(timer); resolve(); });
    });
  }
  const first = await boot();
  let owner;
  try {
    const response = await fetch(`http://127.0.0.1:${first.ready.port}/api/auth/me`,
      { headers: { Authorization: "Bearer " + first.ready.token } });
    assert.equal(response.status, 200);
    owner = (await response.json()).user_id;
    console.log("Authenticated persistent local identity");
    const unauthenticated = await fetch(`http://127.0.0.1:${first.ready.port}/api/materials`);
    assert.equal(unauthenticated.status, 401);
  } finally { await shutdown(first.child); }
  const second = await boot();
  try {
    const response = await fetch(`http://127.0.0.1:${second.ready.port}/api/auth/me`,
      { headers: { Authorization: "Bearer " + second.ready.token } });
    assert.equal((await response.json()).user_id, owner);
    assert.notEqual(first.ready.token, second.ready.token);
    console.log("Restart retained owner and generated a new session");
    // Encrypt synthetic installation credentials for the real Electron-window check.
    if (process.env.STUDY_PREPARE_ELECTRON === "1") {
      const electron = require("../desktop/node_modules/electron");
      const electronEnv = { ...process.env };
      delete electronEnv.ELECTRON_RUN_AS_NODE;
      const fixture = path.join(home, "config", "synthetic-credential-fixture.json");
      await fs.writeFile(fixture, JSON.stringify({ home, keys }), { mode: 0o600 });
      electronEnv.STUDY_FIXTURE_FILE = fixture;
      const helper = spawn(electron, [path.join(root, "tests/desktop_encrypt_fixture.cjs")],
        { windowsHide: true, stdio: ["pipe", "ignore", "pipe"], env: electronEnv });
      helper.stderr.on("data", data => console.error("Encryption fixture:", data.toString().slice(0, 1000)));
      helper.stdin.end();
      try {
        await new Promise((resolve, reject) => helper.once("exit", code => code === 0 ? resolve() :
          reject(new Error("Credential fixture encryption failed"))));
      } finally { await fs.rm(fixture, { force: true }); }
    }
  } finally { await shutdown(second.child); }
  console.log(JSON.stringify({ passed: true, home, probe, scope: "empty directories, minimal PATH, real PostgreSQL/API/Hindsight, owner persistence",
    limitations: "same Windows kernel and installed system libraries; no external provider calls" }));
} finally { server.closeAllConnections(); server.close(); }
