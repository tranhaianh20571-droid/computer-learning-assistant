import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { crc32 } from "node:zlib";
const require = createRequire(import.meta.url);
const { inside, download, extract, installed, validateManifest } = require("../desktop/src/install.cjs");
const { externalUrl } = require("../desktop/src/external.cjs");
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
const item = bytes => ({ id: "core", version: "1", bytes: bytes.length, sha256: sha(bytes),
  unpackedBytes: 1024, url: "https://github.com/tranhaianh20571-droid/computer-learning-assistant/releases/download/v1/core.zip" });

test("external browser URLs accept HTTPS and preserve login fragments only", () => {
  assert.equal(externalUrl("https://market.example.test/#login=synthetic-ticket"),
    "https://market.example.test/#login=synthetic-ticket");
  for (const value of ["file:///C:/Windows/explorer.exe", "javascript:alert(1)", "cmd:/run",
    "http://example.test", "https://user:password@example.test/", null, "https://" + "a".repeat(8200)]) {
    assert.throws(() => externalUrl(value));
  }
});

function zipFixture(entries) {
  const local = [], central = [];
  let offset = 0;
  for (const { name, content = "synthetic-runtime", mode = 0o100644 } of entries) {
    const filename = Buffer.from(name), data = Buffer.from(content), header = Buffer.alloc(30), directory = Buffer.alloc(46);
    header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4);
    header.writeUInt32LE(crc32(data), 14); header.writeUInt32LE(data.length, 18);
    header.writeUInt32LE(data.length, 22); header.writeUInt16LE(filename.length, 26);
    directory.writeUInt32LE(0x02014b50); directory.writeUInt16LE(0x314, 4); directory.writeUInt16LE(20, 6);
    directory.writeUInt32LE(crc32(data), 16); directory.writeUInt32LE(data.length, 20);
    directory.writeUInt32LE(data.length, 24); directory.writeUInt16LE(filename.length, 28);
    directory.writeUInt32LE((mode << 16) >>> 0, 38); directory.writeUInt32LE(offset, 42);
    local.push(header, filename, data); central.push(directory, filename);
    offset += header.length + filename.length + data.length;
  }
  const catalog = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(catalog.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, catalog, end]);
}

test("parallel extraction awaits every file and detects corruption on reuse", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-extract-"));
  try {
    const archive = path.join(directory, "runtime.zip"), target = path.join(directory, "runtime");
    const entries = Array.from({ length: 40 }, (_, index) => ({ name: `lib/${index}.txt`, content: "file-" + index }));
    await fs.writeFile(archive, zipFixture(entries));
    const files = await extract(archive, target, 100000);
    assert.equal(files.length, entries.length);
    await fs.writeFile(path.join(target, ".receipt.json"), JSON.stringify({ sha256: "synthetic", files }));
    assert.equal(await installed(target, { sha256: "synthetic" }), true);
    // A metadata-only change is rehashed and accepted without reinstalling.
    const unchanged = path.join(target, "lib/14.txt");
    await fs.utimes(unchanged, new Date(), new Date(Date.now() + 2000));
    assert.equal(await installed(target, { sha256: "synthetic" }), true);
    await fs.writeFile(path.join(target, "lib/15.txt"), "broken!");
    await fs.utimes(path.join(target, "lib/15.txt"), new Date(), new Date(Date.now() + 3000));
    assert.equal(await installed(target, { sha256: "synthetic" }), false);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("extraction rejects links, escaping entries and declared size overflow", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-extract-boundary-"));
  try {
    let index = 0;
    for (const entries of [[{ name: "../outside.txt" }], [{ name: "link", mode: 0o120777 }], [{ name: "large.txt" }]]) {
      const archive = path.join(directory, `${index}.zip`), target = path.join(directory, String(index++));
      await fs.writeFile(archive, zipFixture(entries));
      await assert.rejects(extract(archive, target, entries[0].name === "large.txt" ? 1 : 100000));
    }
    assert.equal(await fs.stat(path.join(directory, "outside.txt")).catch(() => null), null);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("archive paths cannot escape or use drive/UNC paths", () => {
  for (const file of ["../escape", "/escape", "C:/escape", "a\\b", "//server/share", "a/../../secret"]) {
    assert.throws(() => inside("C:/owned", file));
  }
  assert.ok(inside(os.tmpdir(), "runtime/python.exe").endsWith(path.join("runtime", "python.exe")));
});

test("manifest rejects incomplete, duplicate and foreign downloads", () => {
  const ids = ["core", "postgres", "video", "asr-small", "memory", "classroom"];
  const manifest = { schema: 1, platform: "win32-x64", components: ids.map(id => ({ ...item(Buffer.from("zip")), id })) };
  assert.equal(validateManifest(manifest), manifest);
  assert.throws(() => validateManifest({ ...manifest, components: manifest.components.slice(1) }));
  assert.throws(() => validateManifest({ ...manifest, components: [...manifest.components, manifest.components[0]] }));
  assert.throws(() => validateManifest({ ...manifest, components: [{ ...manifest.components[0],
    url: "https://example.com/core.zip" }, ...manifest.components.slice(1)] }));
});

test("download resumes, verifies, and reuses a completed file", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-download-"));
  try {
    const payload = Buffer.from("verified-component-download"), target = path.join(directory, "core.zip");
    await fs.writeFile(target + ".part", payload.subarray(0, 5));
    let calls = 0;
    const fetcher = async (url, options) => {
      calls++;
      assert.equal(options.headers.Range, "bytes=5-");
      return new Response(payload.subarray(5), { status: 206,
        headers: { "content-range": `bytes 5-${payload.length - 1}/${payload.length}` } });
    };
    await download(item(payload), target, () => {}, fetcher);
    await download(item(payload), target, () => {}, () => { throw Error("should not redownload"); });
    assert.equal(calls, 1);
    assert.deepEqual(await fs.readFile(target), payload);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("a server ignoring Range restarts instead of appending", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-range-"));
  try {
    const payload = Buffer.from("complete"), target = path.join(directory, "data.zip");
    await fs.writeFile(target + ".part", "old");
    await download(item(payload), target, () => {}, async () => new Response(payload));
    assert.deepEqual(await fs.readFile(target), payload);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("bad checksum and oversized downloads cannot be installed", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-checksum-"));
  try {
    const expected = item(Buffer.from("real"));
    await assert.rejects(download(expected, path.join(directory, "bad.zip"), () => {},
      async () => new Response("fake")), /CHECKSUM/);
    await assert.rejects(download(expected, path.join(directory, "large.zip"), () => {},
      async () => new Response("far too long")), /TOO_LARGE/);
    assert.equal(await installed(directory, expected), false);
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});

test("wrong partial response is rejected before appending bytes", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "study-wrong-range-"));
  try {
    const target = path.join(directory, "data.zip"), expected = item(Buffer.from("12345"));
    await fs.writeFile(target + ".part", "12");
    await assert.rejects(download(expected, target, () => {}, async () => new Response("345",
      { status: 206, headers: { "content-range": "bytes 1-3/5" } })), /RANGE_INVALID/);
    assert.equal(await fs.readFile(target + ".part", "utf8"), "12");
  } finally { await fs.rm(directory, { recursive: true, force: true }); }
});
