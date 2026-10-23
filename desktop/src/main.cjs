const { app, BrowserWindow, ipcMain, safeStorage, session, net, dialog } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { randomBytes, createHash } = require("node:crypto");
const http = require("node:http");
const { prepare, validateManifest, atomicJson } = require("./install.cjs");
const { protectHome } = require("./paths.cjs");
const { externalUrl } = require("./external.cjs");
const browsers = require("./browsers.cjs");
const { DesktopAccount } = require("./account.cjs");

function openLink(value) {
  try { window.webContents.send("browser:request", externalUrl(value)); } catch {}
}

let window, child, server, origin, account, accountListener, running = false, quitting = false;
const browserChoices = new Map();
let status = { stage: "checking" };
const resources = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..");
const home = process.env.STUDY_DESKTOP_HOME || (app.isPackaged
  ? path.join(path.dirname(app.getPath("exe")), "userdata") : path.join(resources, "../.backup/desktop-user"));
app.setPath("userData", path.join(home, "config", "electron"));

function notify(value) {
  status = value;
  if (window && !window.isDestroyed()) window.webContents.send("setup:progress", value);
}

function applicationFrame(event) {
  if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame ||
      !origin || new URL(event.senderFrame.url).origin !== origin) throw new Error("EXTERNAL_FRAME_INVALID");
}

async function browserChoice(candidate) {
  const id = createHash("sha256").update(candidate.executable.toLowerCase()).digest("hex").slice(0, 24);
  browserChoices.set(id, candidate.executable);
  const icon = await app.getFileIcon(candidate.executable, { size: "normal" }).then(value => value.toDataURL()).catch(() => "");
  return { id, name: candidate.name, icon };
}

async function secrets() {
  if (!safeStorage.isEncryptionAvailable()) throw new Error("SECURE_STORAGE_UNAVAILABLE");
  const file = path.join(home, "config", "secrets.bin");
  try { return JSON.parse(safeStorage.decryptString(await fs.readFile(file))); }
  catch (error) { if (error.code !== "ENOENT") throw new Error("SECURE_STORAGE_UNAVAILABLE"); }
  const value = Object.fromEntries(["database", "memory", "proxy", "encryption", "session", "gateway"]
    .map(key => [key, randomBytes(32).toString("base64url")]));
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, safeStorage.encryptString(JSON.stringify(value)), { flag: "wx" });
  return value;
}

async function stop() {
  if (accountListener) session.defaultSession.cookies.removeListener("changed", accountListener);
  accountListener = null;
  if (account) await Promise.allSettled([...account.changes]);
  if (account) await account.pending.catch(() => {});
  if (server) { server.closeAllConnections(); server.close(); server = null; }
  if (!child) return;
  const owned = child;
  child = null;
  const done = new Promise(resolve => owned.once("exit", resolve));
  owned.stdin.end();
  await Promise.race([done, new Promise(resolve => setTimeout(resolve, 35000))]);
  if (owned.exitCode === null) owned.kill();
}

async function serve(frontend, port, gateway) {
  const mime = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".wasm": "application/wasm",
    ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".ttf": "font/ttf" };
  server = http.createServer(async (req, res) => {
    try {
      if (req.headers.host !== new URL(origin).host ||
          req.headers["x-desktop-instance"] !== gateway ||
          (req.headers.origin && req.headers.origin !== origin)) {
        res.writeHead(403); res.end(); return;
      }
      if (req.url.startsWith("/api/") || req.url.startsWith("/tasks")) {
        const headers = { ...req.headers, host: `127.0.0.1:${port}`, origin: `http://127.0.0.1:${port}` };
        delete headers["x-desktop-instance"];
        const upstream = http.request({ hostname: "127.0.0.1", port, path: req.url, method: req.method, headers },
          response => { res.writeHead(response.statusCode, response.headers); response.pipe(res); });
        upstream.on("error", () => { if (!res.headersSent) res.writeHead(502); res.end(); });
        req.on("aborted", () => upstream.destroy());
        res.on("close", () => upstream.destroy());
        req.pipe(upstream);
      } else {
        const pathname = decodeURIComponent(new URL(req.url, origin).pathname);
        const file = path.resolve(frontend, "." + (pathname === "/" ? "/index.html" : pathname));
        if (!file.startsWith(path.resolve(frontend) + path.sep)) { res.writeHead(404); res.end(); return; }
        const data = await fs.readFile(file);
        res.setHeader("Content-Type", mime[path.extname(file)] || "application/octet-stream");
        res.setHeader("X-Content-Type-Options", "nosniff");
        res.end(req.method === "HEAD" ? undefined : data);
      }
    } catch { if (!res.headersSent) res.writeHead(404); res.end(); }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject); server.listen(0, "127.0.0.1", resolve);
  });
  origin = `http://127.0.0.1:${server.address().port}`;
  session.defaultSession.webRequest.onBeforeSendHeaders({ urls: [origin + "/*"] }, (details, callback) => {
    // Only this Electron session receives the instance authentication header.
    callback({ requestHeaders: { ...details.requestHeaders, "X-Desktop-Instance": gateway } });
  });
}

async function start() {
  if (running) return;
  running = true;
  try {
    await stop();
    await fs.mkdir(home, { recursive: true });
    await protectHome(home);
    const manifestFile = app.isPackaged ? path.join(resources, "runtime.json") :
      path.join(resources, "manifests/runtime.json");
    const manifest = validateManifest(JSON.parse(await fs.readFile(manifestFile, "utf8")));
    const roots = await prepare(manifest, home, notify, { fetcher: (url, options) => net.fetch(url, options) });
    const keys = await secrets();
    notify({ stage: "starting", component: "postgres" });
    const backend = app.isPackaged ? path.join(resources, "backend") : path.resolve(resources, "../server/backend");
    const worker = app.isPackaged ? path.join(resources, "source-worker") : path.resolve(resources, "../server/source-worker");
    const memoryScript = app.isPackaged ? path.join(resources, "memory_service.py") :
      path.resolve(resources, "../deploy/memory_service.py");
    const env = Object.fromEntries(["SystemRoot", "WINDIR", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA"]
      .filter(key => process.env[key]).map(key => [key, process.env[key]]));
    env.PYTHONPATH = backend;
    env.PYTHONNOUSERSITE = "1";
    env.PYTHONDONTWRITEBYTECODE = "1";
    env.PYTHONUTF8 = "1";
    env.TEMP = env.TMP = path.join(home, "cache", "tmp");
    await fs.mkdir(env.TEMP, { recursive: true });
    child = spawn(path.join(roots.classroom, "sandbox", "Sandbox.exe"), ["--supervise"], {
      cwd: home, env, windowsHide: true, stdio: ["pipe", "pipe", "ignore"]
    });
    const owned = child;
    owned.stdin.write(JSON.stringify({
      executable: path.join(roots.core, "python.exe"), args: ["-u", "-m", "app.desktop_runtime"],
      directory: home, owner: process.pid, payload: { home, roots, keys, backend, worker, memoryScript }
    }) + "\n");
    const ready = await new Promise((resolve, reject) => {
      let buffer = "";
      const timer = setTimeout(() => reject(new Error("SERVICE_FAILED")), 600000);
      owned.once("error", () => { clearTimeout(timer); reject(new Error("SERVICE_FAILED")); });
      owned.once("exit", () => { clearTimeout(timer); reject(new Error("SERVICE_FAILED")); });
      owned.stdout.on("data", data => {
        buffer += data.toString();
        if (buffer.length > 100000) { reject(new Error("SERVICE_FAILED")); return; }
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
          try {
            const value = JSON.parse(line);
            if (value.stage === "ready" && Number.isInteger(value.port)) {
              clearTimeout(timer); resolve(value);
            } else if (value.stage === "failed") {
              clearTimeout(timer); reject(new Error(value.code || "SERVICE_FAILED"));
            } else if (value.stage === "starting") notify({ stage: "starting", component: value.component });
          } catch {}
        }
      });
    });
    await serve(app.isPackaged ? path.join(resources, "frontend") : path.resolve(resources, "../frontend/dist"),
      ready.port, keys.gateway);
    account = new DesktopAccount(home, safeStorage, session.defaultSession.cookies, token => new Promise((resolve, reject) => {
      const request = http.get({ hostname: "127.0.0.1", port: ready.port, path: "/api/auth/me",
        headers: { Authorization: `Bearer ${token}` }, timeout: 10000 }, response => {
        response.resume();
        if (response.statusCode === 200) resolve(true);
        else if (response.statusCode === 401) resolve(false);
        else reject(new Error("ACCOUNT_RESTORE_FAILED"));
      });
      request.on("timeout", () => request.destroy(new Error("ACCOUNT_RESTORE_FAILED")));
      request.on("error", () => reject(new Error("ACCOUNT_RESTORE_FAILED")));
    }));
    const signedIn = await account.restore(origin, ready.token);
    accountListener = (_event, cookie, cause, removed) => {
      void Promise.resolve(account.changed(cookie, removed, cause)).catch(() => notify({
        stage: "ready", code: "ACCOUNT_SAVE_FAILED"
      }));
    };
    session.defaultSession.cookies.on("changed", accountListener);
    await window.loadURL(origin + (signedIn ? "/#/home" : "/#/login"));
    notify({ stage: "ready" });
    if (app.isPackaged) {
      const { autoUpdater } = require("electron-updater");
      autoUpdater.logger = null;
      autoUpdater.on("error", () => {});
      autoUpdater.allowPrerelease = false;
      autoUpdater.autoDownload = true;
      autoUpdater.autoInstallOnAppQuit = true;
      void autoUpdater.checkForUpdates().catch(() => {});
    }
    owned.once("exit", () => {
      if (!quitting) {
        void window.loadFile(path.join(__dirname, "setup.html"));
        notify({ stage: "failed", code: "SERVICE_FAILED" });
      }
    });
    await atomicJson(path.join(home, "config", "version.json"), { version: app.getVersion(), runtime: manifest.version });
  } catch (error) {
    await stop();
    notify({ stage: "failed", code: /^[A-Z_]+$/.test(error.message) ? error.message : "SERVICE_FAILED" });
  } finally { running = false; }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { if (window) { window.restore(); window.focus(); } });
  app.whenReady().then(() => {
    window = new BrowserWindow({ width: 1400, height: 920, minWidth: 720, minHeight: 560,
      icon: path.join(__dirname, "../assets/icon.ico"),
      title: "学习助手伙伴", backgroundColor: "#f4f5f3",
      webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, sandbox: true, nodeIntegration: false } });
    window.setMenuBarVisibility(false);
    window.webContents.setWindowOpenHandler(({ url }) => {
      if (url.startsWith("https://")) openLink(url);
      return { action: "deny" };
    });
    window.webContents.on("will-navigate", (event, url) => {
      if (!origin || !url.startsWith(origin + "/")) {
        event.preventDefault();
        if (url.startsWith("https://")) openLink(url);
      }
    });
    ipcMain.handle("setup:status", () => status);
    ipcMain.handle("browser:list", async event => {
      applicationFrame(event);
      const script = app.isPackaged ? path.join(process.resourcesPath, "browser-tools/detect-browsers.ps1") : undefined;
      return Promise.all((await browsers.discoverBrowsers(script)).map(browserChoice));
    });
    ipcMain.handle("browser:choose", async event => {
      applicationFrame(event);
      const selected = await dialog.showOpenDialog(window, {
        title: "选择浏览器程序", properties: ["openFile"], filters: [{ name: "浏览器程序", extensions: ["exe"] }]
      });
      if (selected.canceled || !selected.filePaths[0]) return null;
      const executable = selected.filePaths[0];
      if (!await browsers.executableFile(executable)) throw new Error("请选择有效的浏览器程序。");
      return browserChoice({ name: path.basename(executable, ".exe"), executable });
    });
    ipcMain.handle("browser:open", async (event, url, id) => {
      applicationFrame(event);
      const executable = browserChoices.get(id);
      if (!executable) throw new Error("请选择电脑上的浏览器。");
      const target = externalUrl(url);
      try { await browsers.launchBrowser(executable, target); }
      catch { throw new Error("无法启动所选浏览器，请尝试其他浏览器。"); }
    });
    ipcMain.handle("setup:retry", event => {
      if (event.senderFrame.url === require("node:url").pathToFileURL(path.join(__dirname, "setup.html")).href) void start();
    });
    void window.loadFile(path.join(__dirname, "setup.html")).then(start);
  });
  app.on("before-quit", event => {
    if (quitting) return;
    event.preventDefault(); quitting = true;
    notify({ stage: "stopping" });
    void stop().finally(() => app.quit());
  });
  app.on("window-all-closed", () => app.quit());
}
