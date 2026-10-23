import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { executableFile, launchBrowser } = require("../desktop/src/browsers.cjs");

test("browser selection requires an existing executable file", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-browser-"));
  try {
    const exe = path.join(directory, "browser.exe");
    await fs.writeFile(exe, "MZsynthetic-pe-fixture");
    assert.equal(await executableFile(exe), true);
    assert.equal(await executableFile("browser.exe"), false);
    assert.equal(await executableFile(path.join(directory, "missing.exe")), false);
    const script = path.join(directory, "browser.cmd");
    await fs.writeFile(script, "MZsynthetic");
    assert.equal(await executableFile(script), false);
    await fs.writeFile(exe, "not-a-pe");
    assert.equal(await executableFile(exe), false);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("launch uses the selected program and exactly one HTTPS argument without a shell", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-browser-args-"));
  const old = process.env.STUDY_SYNTHETIC_BROWSER_SECRET;
  try {
    process.env.STUDY_SYNTHETIC_BROWSER_SECRET = "synthetic-do-not-inherit";
    const exe = path.join(directory, "user-selected.exe");
    await fs.writeFile(exe, "MZsynthetic");
    let unreferenced = false;
    await launchBrowser(exe, "https://market.example.test/#login=synthetic-ticket", (file, args, options) => {
      assert.equal(file, exe);
      assert.deepEqual(args, ["https://market.example.test/#login=synthetic-ticket"]);
      assert.equal(options.shell, false);
      assert.equal(options.env.STUDY_SYNTHETIC_BROWSER_SECRET, undefined);
      const child = new EventEmitter();
      child.unref = () => { unreferenced = true; };
      queueMicrotask(() => child.emit("spawn"));
      return child;
    });
    assert.equal(unreferenced, true);
    await assert.rejects(launchBrowser(exe, "file:///C:/Windows/explorer.exe",
      () => { throw new Error("Invalid scheme must never launch"); }), /EXTERNAL_URL_INVALID/);
    await assert.rejects(launchBrowser(exe, "https://market.example.test/", () => {
      const child = new EventEmitter();
      queueMicrotask(() => child.emit("error", new Error("synthetic-launch-failure")));
      return child;
    }), /synthetic-launch-failure/);
  } finally {
    if (old === undefined) delete process.env.STUDY_SYNTHETIC_BROWSER_SECRET;
    else process.env.STUDY_SYNTHETIC_BROWSER_SECRET = old;
    await fs.rm(directory, { recursive: true, force: true });
  }
});
