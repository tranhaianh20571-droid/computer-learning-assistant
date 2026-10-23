const fs = require("node:fs/promises");
const path = require("node:path");

class DesktopAccount {
  constructor(home, storage, cookies, validate) {
    this.file = path.join(home, "config", "account.bin");
    this.storage = storage;
    this.cookies = cookies;
    this.validate = validate;
    this.pending = Promise.resolve();
    this.changes = new Set();
    this.revision = 0;
  }

  save(token) {
    this.pending = this.pending.catch(() => {}).then(async () => {
      const bytes = this.storage.encryptString(JSON.stringify({ schema: 1, token }));
      await fs.writeFile(this.file + ".tmp", bytes);
      await fs.rename(this.file + ".tmp", this.file);
    });
    return this.pending;
  }

  async restore(origin, initialToken) {
    let saved;
    try {
      saved = JSON.parse(this.storage.decryptString(await fs.readFile(this.file)));
      if (saved.schema !== 1 || !(saved.token === null || typeof saved.token === "string"))
        throw new Error("ACCOUNT_RESTORE_FAILED");
    } catch (error) {
      if (error.code !== "ENOENT") throw new Error("ACCOUNT_RESTORE_FAILED");
    }
    const legacy = saved ? null : (await this.cookies.get({ url: origin, name: "session_token" }))[0];
    const candidate = saved ? saved.token : legacy?.value || initialToken;
    const token = candidate && await this.validate(candidate) ? candidate : null;
    await this.save(token);
    if (token) {
      await this.cookies.set({ url: origin, name: "session_token", value: token,
        httpOnly: true, sameSite: "lax", path: "/", expirationDate: Date.now() / 1000 + 14 * 86400 });
    } else await this.cookies.remove(origin, "session_token");
    return !!token;
  }

  changed(cookie, removed, cause) {
    if (cookie.name !== "session_token" || cookie.domain !== "127.0.0.1" || cookie.path !== "/") return;
    // Cookie replacement emits a removal followed by an insertion.
    if (removed && cause === "overwrite") return;
    const revision = ++this.revision;
    const work = (async () => {
      const valid = !removed && await this.validate(cookie.value);
      if (revision === this.revision && (removed || valid))
        await this.save(removed ? null : cookie.value);
    })();
    this.changes.add(work);
    work.then(() => this.changes.delete(work), () => this.changes.delete(work));
    return work;
  }
}

module.exports = { DesktopAccount };
