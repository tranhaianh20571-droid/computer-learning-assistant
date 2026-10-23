// Compare shipped dependency paths with the complete built runtimes. A broad
// exclusion rule must not silently remove a package's internal directories.
import fs from "node:fs/promises";
import path from "node:path";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(path.resolve(import.meta.dirname, "../desktop/package.json"));
const yauzl = require("yauzl");
const root = path.resolve(import.meta.dirname, "..");
const manifest = JSON.parse(await fs.readFile(path.join(root, "desktop/manifests/runtime.json")));
for (const item of manifest.components) {
  const directory = path.join(root, ".backup/desktop-build", item.id);
  const archive = path.join(root, ".backup/desktop-components", path.basename(new URL(item.url).pathname));
  const expected = new Set((await fs.readdir(directory, { recursive: true, withFileTypes: true }))
    .filter(entry => entry.isFile())
    .map(entry => path.relative(directory, path.join(entry.parentPath, entry.name)).replaceAll("\\", "/"))
    .filter(name => !name.endsWith(".pyc") &&
      !name.split("/").some(part => part.startsWith(".sandbox") || part.startsWith(".preflight"))));
  const actual = await new Promise((resolve, reject) => {
    yauzl.open(archive, { lazyEntries: true }, (error, zip) => {
      if (error) return reject(error);
      const files = new Set();
      zip.on("error", reject);
      zip.on("entry", entry => {
        if (!entry.fileName.endsWith("/") && !entry.fileName.endsWith(".pyc")) files.add(entry.fileName);
        zip.readEntry();
      });
      zip.on("end", () => resolve(files));
      zip.readEntry();
    });
  });
  const missing = [...expected].filter(file => !actual.has(file));
  const unexpected = [...actual].filter(file => !expected.has(file));
  assert.deepEqual({ missing, unexpected }, { missing: [], unexpected: [] }, item.id);
  console.log(`Archive dependency paths verified: ${item.id} (${actual.size} files)`);
}
