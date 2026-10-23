const { app, safeStorage } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");
const { home, keys } = JSON.parse(require("node:fs").readFileSync(process.env.STUDY_FIXTURE_FILE, "utf8"));
app.setPath("userData", path.join(home, "config/electron"));
app.whenReady().then(async () => {
  if (!safeStorage.isEncryptionAvailable()) { app.exit(1); return; }
  await fs.writeFile(path.join(home, "config/secrets.bin"), safeStorage.encryptString(JSON.stringify(keys)), { flag: "wx" });
  app.quit();
}).catch(() => app.exit(1));
