const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const { pipeline } = require("node:stream/promises");
const { Transform } = require("node:stream");
const yauzl = require("yauzl");

function inside(root, name) {
  if (typeof name !== "string" || !name || /[\\:\0]/.test(name) || name.startsWith("/")) {
    throw new Error("ARCHIVE_PATH_INVALID");
  }
  const target = path.resolve(root, name);
  if (!target.startsWith(path.resolve(root) + path.sep)) throw new Error("ARCHIVE_PATH_INVALID");
  return target;
}

async function digest(file) {
  const hash = crypto.createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest("hex");
}

async function atomicJson(file, value) {
  await fsp.mkdir(path.dirname(file), { recursive: true });
  const temporary = file + ".part";
  await fsp.writeFile(temporary, JSON.stringify(value), { mode: 0o600 });
  await fsp.rename(temporary, file);
}

function validateManifest(manifest) {
  if (manifest.schema !== 1 || manifest.platform !== "win32-x64" || !Array.isArray(manifest.components)) {
    throw new Error("MANIFEST_INVALID");
  }
  const ids = new Set();
  for (const item of manifest.components) {
    if (!/^[a-z][a-z0-9-]{0,40}$/.test(item.id) || ids.has(item.id) ||
        !/^[a-zA-Z0-9._-]+$/.test(item.version) ||
        !/^[a-f0-9]{64}$/.test(item.sha256) || !Number.isSafeInteger(item.bytes) || item.bytes <= 0 ||
        !Number.isSafeInteger(item.unpackedBytes) || item.unpackedBytes < item.bytes) {
      throw new Error("MANIFEST_INVALID");
    }
    ids.add(item.id);
    const url = new URL(item.url);
    if (url.protocol !== "https:" || url.hostname !== "github.com" ||
        !url.pathname.startsWith("/tranhaianh20571-droid/computer-learning-assistant/releases/download/")) {
      throw new Error("DOWNLOAD_SOURCE_INVALID");
    }
  }
  for (const required of ["core", "postgres", "video", "asr-small", "memory", "classroom"]) {
    if (!ids.has(required)) throw new Error("MANIFEST_INCOMPLETE");
  }
  return manifest;
}

async function download(item, target, progress, fetcher = fetch) {
  const partial = target + ".part";
  await fsp.mkdir(path.dirname(target), { recursive: true });
  if (await digest(target).catch(() => "") === item.sha256) return target;
  let offset = await fsp.stat(partial).then(stat => stat.size).catch(() => 0);
  if (offset > item.bytes) { await fsp.rm(partial, { force: true }); offset = 0; }
  const response = await fetcher(item.url, {
    headers: offset ? { Range: `bytes=${offset}-` } : {},
    signal: AbortSignal.timeout(30 * 60 * 1000)
  });
  if (offset && response.status === 416 && offset === item.bytes) {
    // A completed transfer may have been interrupted just before verification.
  } else {
    if (!response.ok || !response.body) throw new Error("DOWNLOAD_FAILED");
    if (response.status === 206) {
      const range = response.headers.get("content-range");
      if (!range?.startsWith(`bytes ${offset}-`) || !range.endsWith(`/${item.bytes}`)) {
        throw new Error("DOWNLOAD_RANGE_INVALID");
      }
    } else offset = 0;
    let received = offset;
    await pipeline(response.body, new Transform({
      transform(chunk, encoding, callback) {
        received += chunk.length;
        if (received > item.bytes) return callback(new Error("DOWNLOAD_TOO_LARGE"));
        progress(received, item.bytes);
        callback(null, chunk);
      }
    }), fs.createWriteStream(partial, { flags: offset ? "a" : "w" }));
  }
  if ((await fsp.stat(partial)).size !== item.bytes || await digest(partial) !== item.sha256) {
    await fsp.rm(partial, { force: true });
    throw new Error("DOWNLOAD_CHECKSUM_FAILED");
  }
  await fsp.rename(partial, target);
  return target;
}

function extract(archive, root, limit) {
  return new Promise((resolve, reject) => {
    yauzl.open(archive, { lazyEntries: true, autoClose: false }, (error, zip) => {
      if (error) return reject(error);
      let unpacked = 0, entries = 0, active = 0, reading = false, ended = false, failure;
      const files = [], directories = new Map();
      const mkdir = directory => {
        if (!directories.has(directory)) directories.set(directory, fsp.mkdir(directory, { recursive: true }));
        return directories.get(directory);
      };
      const fail = error => { failure ||= error; zip.close(); pump(); };
      const pump = () => {
        if (failure) { if (!active) reject(failure); return; }
        if (ended) { if (!active) { zip.close(); resolve(files); } return; }
        if (!reading && active < 8) { reading = true; zip.readEntry(); }
      };
      zip.on("error", fail);
      zip.on("end", () => { ended = true; reading = false; pump(); });
      zip.on("entry", entry => {
        reading = false;
        active++;
        const finish = error => { active--; if (error) fail(error); else pump(); };
        void Promise.resolve().then(async () => {
          const mode = entry.externalFileAttributes >>> 16;
          if ((mode & 0o170000) === 0o120000) throw new Error("ARCHIVE_LINK_INVALID");
          const output = inside(root, entry.fileName);
          unpacked += entry.uncompressedSize;
          if (unpacked > limit || ++entries > 150000) throw new Error("ARCHIVE_TOO_LARGE");
          if (entry.fileName.endsWith("/")) await mkdir(output);
          else {
            await mkdir(path.dirname(output));
            const input = await new Promise((done, failed) => zip.openReadStream(entry,
              (err, stream) => err ? failed(err) : done(stream)));
            const hash = crypto.createHash("sha256");
            await pipeline(input, new Transform({
              transform(chunk, encoding, callback) { hash.update(chunk); callback(null, chunk); }
            }), fs.createWriteStream(output, { flags: "wx" }));
            const modified = entry.getLastModDate();
            await fsp.utimes(output, modified, modified);
            const { mtimeMs } = await fsp.stat(output);
            files.push({ name: entry.fileName, bytes: entry.uncompressedSize, mtimeMs, sha256: hash.digest("hex") });
          }
        }).then(() => finish(), finish);
        pump();
      });
      pump();
    });
  });
}

async function installed(root, item) {
  try {
    const receipt = JSON.parse(await fsp.readFile(path.join(root, ".receipt.json"), "utf8"));
    if (receipt.sha256 !== item.sha256 || !receipt.files.length) return false;
    let next = 0, valid = true, updated = false;
    await Promise.all(Array.from({ length: 8 }, async () => {
      while (valid && next < receipt.files.length) {
        const file = receipt.files[next++];
        const target = inside(root, file.name);
        const stat = await fsp.lstat(target);
        if (!stat.isFile() || stat.size !== file.bytes) { valid = false; continue; }
        if (stat.mtimeMs !== file.mtimeMs) {
          if (await digest(target) !== file.sha256) { valid = false; continue; }
          file.mtimeMs = stat.mtimeMs;
          updated = true;
        }
      }
    }));
    if (valid && updated) await atomicJson(path.join(root, ".receipt.json"), receipt);
    return valid;
  } catch { return false; }
}

async function prepare(manifest, home, notify, options = {}) {
  const roots = {};
  for (const item of manifest.components) {
    const directory = path.join(home, "runtimes", `${item.id}-${item.sha256.slice(0, 16)}`);
    roots[item.id] = directory;
    notify({ stage: "checking", component: item.id });
    if (await installed(directory, item)) continue;
    const space = await fsp.statfs(home);
    if (space.bavail * space.bsize < item.bytes + item.unpackedBytes + 256 * 1024 * 1024) {
      throw new Error("DISK_SPACE_LOW");
    }
    let archive;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        notify({ stage: "downloading", component: item.id, attempt });
        archive = await download(item, path.join(home, "cache", `${item.sha256}.zip`),
          (current, total) => notify({ stage: "downloading", component: item.id, current, total }), options.fetcher);
        break;
      } catch (error) {
        if (attempt === 2) {
          if (error.code === "ENOSPC") throw new Error("DISK_SPACE_LOW");
          throw new Error(/^DOWNLOAD_[A-Z_]+$/.test(error.message) ? error.message : "DOWNLOAD_FAILED");
        }
        await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
      }
    }
    notify({ stage: "extracting", component: item.id });
    const staging = directory + ".staging";
    await fsp.rm(staging, { recursive: true, force: true });
    await fsp.mkdir(staging, { recursive: true });
    const files = await extract(archive, staging, item.unpackedBytes);
    await atomicJson(path.join(staging, ".receipt.json"), { sha256: item.sha256, files });
    if (fs.existsSync(directory)) await fsp.rm(directory, { recursive: true });
    await fsp.rename(staging, directory);
  }
  return roots;
}

module.exports = { inside, digest, atomicJson, validateManifest, download, extract, installed, prepare };
