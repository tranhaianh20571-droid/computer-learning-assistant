const { BrowserWindow, ipcMain, session, dialog } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");

function installClassroomMedia({ window, origin, applicationFrame }) {
  let captureUntil = 0, exporting = false;
  let closeAllowed = false, closeTimer, closePromise, resolveClose;
  const documents = new Map();
  const finishClose = () => {
    clearTimeout(closeTimer);closeAllowed = true;
    resolveClose?.();
  };
  const beforeClose = () => {
    if (closeAllowed || !origin() || window.isDestroyed() ||
        !window.webContents.getURL().startsWith(origin() + "/")) return Promise.resolve();
    if (!closePromise) {
      closePromise = new Promise(resolve => { resolveClose = resolve; });
      window.webContents.send("classroom:before-close");
      closeTimer = setTimeout(finishClose, 8000);
    }
    return closePromise;
  };
  window.on("close", event => {
    if (closeAllowed || !origin() || !window.webContents.getURL().startsWith(origin() + "/")) return;
    event.preventDefault();
    void beforeClose().then(() => { if (!window.isDestroyed()) window.close(); });
  });
  ipcMain.on("classroom:close-ready", event => {
    try { applicationFrame(event); } catch { return; }
    if (closePromise) finishClose();
  });
  ipcMain.handle("classroom:capture", event => {
    applicationFrame(event);
    if (!new URL(event.senderFrame.url).hash.startsWith("#/lecture/")) throw Error("CAPTURE_NOT_ALLOWED");
    captureUntil = Date.now() + 10000;
  });
  session.defaultSession.setDisplayMediaRequestHandler((request, callback) => {
    if (request.frame !== window.webContents.mainFrame || new URL(request.securityOrigin).origin !== origin() ||
        Date.now() > captureUntil || !request.videoRequested || request.audioRequested) {
      callback({}); return;
    }
    captureUntil = 0;
    callback({ video: request.frame });
  });
  ipcMain.handle("classroom:export-read", event => {
    const item = documents.get(event.sender.id);
    if (!item || event.senderFrame !== event.sender.mainFrame ||
        new URL(event.senderFrame.url).origin !== origin()) throw Error("EXPORT_NOT_ALLOWED");
    return item.payload;
  });
  ipcMain.handle("classroom:export-ready", event => {
    const item = documents.get(event.sender.id);
    if (!item || event.senderFrame !== event.sender.mainFrame ||
        new URL(event.senderFrame.url).origin !== origin()) throw Error("EXPORT_NOT_ALLOWED");
    item.resolve();
  });
  ipcMain.handle("classroom:export", async (event, payload, format) => {
    applicationFrame(event);
    if (!["png", "pdf"].includes(format) || exporting || !payload || !Array.isArray(payload.scenes) ||
        payload.scenes.length > 240 || JSON.stringify(payload).length > 8 * 1024 * 1024) throw Error("EXPORT_INVALID");
    const choice = await dialog.showSaveDialog(window, {
      title: "导出板书", defaultPath: `lecture-board.${format}`,
      filters: [{ name: format.toUpperCase(), extensions: [format] }]
    });
    if (choice.canceled || !choice.filePath) return { saved: false };
    exporting = true;
    const output = new BrowserWindow({ show: false, width: 1100, height: 1500,
      webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true,
        sandbox: true, nodeIntegration: false, backgroundThrottling: false } });
    let timeout;
    const temporary = `${choice.filePath}.part`;
    try {
      const ready = new Promise((resolve, reject) => {
        documents.set(output.webContents.id, { payload, resolve });
        timeout = setTimeout(() => reject(Error("EXPORT_TIMEOUT")), 35000);
      });
      output.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      output.webContents.on("will-navigate", (navigation, url) => {
        if (new URL(url).origin !== origin()) navigation.preventDefault();
      });
      await output.loadURL(origin() + "/#/board-export");
      await ready;
      let bytes;
      if (format === "pdf") {
        bytes = await output.webContents.printToPDF({ printBackground: true, preferCSSPageSize: true });
      } else {
        const size = await output.webContents.executeJavaScript("({height:document.querySelector('.board-export-document').scrollHeight})");
        if (!Number.isFinite(size.height) || size.height > 16000) throw Error("EXPORT_TOO_LARGE");
        output.setContentSize(1100, Math.max(100, Math.ceil(size.height)));
        await new Promise(resolve => setTimeout(resolve, 120));
        bytes = (await output.webContents.capturePage()).toPNG();
      }
      await fs.writeFile(temporary, bytes);
      await fs.rename(temporary, choice.filePath);
      return { saved: true };
    } finally {
      clearTimeout(timeout);
      documents.delete(output.webContents.id);
      output.destroy();
      exporting = false;
      await fs.unlink(temporary).catch(() => {});
    }
  });
  return { beforeClose };
}
module.exports = { installClassroomMedia };
