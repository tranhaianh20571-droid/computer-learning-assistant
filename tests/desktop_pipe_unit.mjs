import assert from "node:assert/strict";
import { test } from "node:test";
import { spawn } from "node:child_process";
import path from "node:path";

test("owner-pipe watching permits native imports and exits on owner closure",
  { skip: process.platform !== "win32" }, async () => {
    const root = path.resolve(import.meta.dirname, "..");
    const executable = process.env.TEST_RUNTIME_PYTHON || path.join(root, ".backup/desktop-build/memory/python.exe");
    const code = "import threading; from app.desktop_lifecycle import wait_for_owner; " +
      "worker=threading.Thread(target=wait_for_owner);worker.start();" +
      "import numpy;print('numpy-ready',flush=True);worker.join()";
    const env = { SystemRoot: process.env.SystemRoot, PATH: path.join(process.env.SystemRoot, "System32"),
      PYTHONPATH: path.join(root, "server/backend"), PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" };
    const child = spawn(executable, ["-c", code], { env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    let output = "", errors = "";
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { child.kill(); reject(new Error("Native import/owner shutdown stalled")); }, 20000);
      child.stdout.on("data", part => {
        output += part.toString();
        if (output.includes("numpy-ready")) child.stdin.end();
      });
      child.stderr.on("data", part => { errors += part.toString(); });
      child.once("error", error => { clearTimeout(timer); reject(error); });
      child.once("exit", code => {
        clearTimeout(timer);
        try { assert.equal(code, 0, errors); assert.match(output, /numpy-ready/); resolve(); }
        catch (error) { reject(error); }
      });
    });
  });
