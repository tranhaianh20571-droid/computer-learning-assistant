import fs from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";

const root = path.resolve(import.meta.dirname, "../..");
const output = path.join(root, ".backup/desktop-dist/SHA256SUMS.txt");
const files = [];
const directories=process.argv.includes("--application-only")?[".backup/desktop-dist"]:
  [".backup/desktop-components", ".backup/desktop-dist"];
for (const directory of directories) {
  for (const name of await fs.readdir(path.join(root, directory))) {
    if (/\.zip$|Setup\.exe$|\.blockmap$|^runtime\.json$|^latest\.yml$/.test(name))
      files.push({ name, file: path.join(root, directory, name) });
  }
}
const lines = [];
for (const { name, file } of files.sort((a, b) => a.name.localeCompare(b.name))) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  lines.push(hash.digest("hex") + "  " + name);
}
await fs.writeFile(output, lines.join("\n") + "\n");
console.log("Release checksums generated:", lines.length);
