import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";
const { DesktopAccount } = createRequire(import.meta.url)("../desktop/src/account.cjs");
const storage = {
  encryptString: text => Buffer.from(text).reverse(),
  decryptString: bytes => Buffer.from(bytes).reverse().toString()
};
const origin = "http://127.0.0.1:12345";
const cookie = value => ({ name: "session_token", domain: "127.0.0.1", path: "/", value });
async function fixture(run) {
  const home = await fs.mkdtemp(path.join(os.tmpdir(), "study-account-"));
  await fs.mkdir(path.join(home, "config"));
  const state = { token: null };
  const cookies = {
    get: async () => state.token ? [cookie(state.token)] : [],
    set: async item => { state.token = item.value; },
    remove: async () => { state.token = null; }
  };
  const account = new DesktopAccount(home, storage, cookies, async () => true);
  try { await run({ home, state, cookies, account }); }
  finally { await fs.rm(home, { recursive: true, force: true }); }
}

test("last signed-in account survives a changed origin and bootstrap token", () => fixture(async ({ home, state, cookies, account }) => {
  await account.restore(origin, "bootstrap-user");
  await account.changed(cookie("last-signed-in-user"), false, "explicit");
  const stored = await fs.readFile(account.file);
  assert(!stored.toString().includes("last-signed-in-user"));
  const restarted = new DesktopAccount(home, storage, cookies, async token => token === "last-signed-in-user");
  await restarted.restore("http://127.0.0.1:54321", "another-bootstrap-token");
  assert.equal(state.token, "last-signed-in-user");
}));

test("existing Electron login is adopted on first upgrade", () => fixture(async ({ state, account }) => {
  state.token = "previous-version-login";
  await account.restore(origin, "bootstrap-user");
  assert.equal(state.token, "previous-version-login");
}));

test("logout, revocation and expiry never switch to bootstrap account", () => fixture(async ({ home, state, cookies, account }) => {
  await account.restore(origin, "bootstrap-user");
  await account.changed(cookie("expired-user"), false, "explicit");
  const restarted = new DesktopAccount(home, storage, cookies, async () => false);
  assert.equal(await restarted.restore(origin, "bootstrap-user"), false);
  assert.equal(state.token, null);
  await restarted.changed(cookie("last-user"), true, "explicit");
  const again = new DesktopAccount(home, storage, cookies, async () => true);
  assert.equal(await again.restore(origin, "bootstrap-user"), false);
}));

test("transient validation failure preserves the stored account for retry", () => fixture(async ({ home, state, cookies, account }) => {
  await account.restore(origin, "last-user");
  const broken = new DesktopAccount(home, storage, cookies, async () => { throw Error("offline"); });
  await assert.rejects(broken.restore(origin, "bootstrap-user"), /offline/);
  assert.equal(state.token, "last-user");
  assert.equal(await account.restore(origin, "bootstrap-user"), true);
}));

test("late cookie validation cannot overwrite logout or a newer login", () => fixture(async ({ account }) => {
  await account.restore(origin, "initial");
  let finish;
  account.validate = token => token === "slow" ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(true);
  const old = account.changed(cookie("slow"), false, "explicit");
  await account.changed(cookie("slow"), true, "explicit");
  await account.changed(cookie("new-login"), false, "explicit");
  finish(true);
  await old;
  assert.equal(JSON.parse(storage.decryptString(await fs.readFile(account.file))).token, "new-login");
  await account.changed(cookie("new-login"), true, "overwrite");
  assert.equal(JSON.parse(storage.decryptString(await fs.readFile(account.file))).token, "new-login");
}));
