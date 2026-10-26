const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("desktopClassroom", {
  allowCapture: () => ipcRenderer.invoke("classroom:capture"),
  exportBoard: (payload, format) => ipcRenderer.invoke("classroom:export", payload, format),
  readExport: () => ipcRenderer.invoke("classroom:export-read"),
  exportReady: () => ipcRenderer.invoke("classroom:export-ready"),
  onBeforeClose: callback => {
    const listener = () => {
      Promise.resolve().then(callback).catch(() => {}).finally(() => ipcRenderer.send("classroom:close-ready"));
    };
    ipcRenderer.on("classroom:before-close", listener);
    return () => ipcRenderer.removeListener("classroom:before-close", listener);
  }
});
contextBridge.exposeInMainWorld("desktopBrowser", {
  listBrowsers: () => ipcRenderer.invoke("browser:list"),
  chooseExecutable: () => ipcRenderer.invoke("browser:choose"),
  openExternal: (url, id) => ipcRenderer.invoke("browser:open", url, id),
  onOpen: callback => {
    const listener = (_, url) => callback(url);
    ipcRenderer.on("browser:request", listener);
    return () => ipcRenderer.removeListener("browser:request", listener);
  }
});
contextBridge.exposeInMainWorld("desktopSetup", {
  status: () => ipcRenderer.invoke("setup:status"),
  retry: () => ipcRenderer.invoke("setup:retry"),
  subscribe: callback => {
    const listener = (_, value) => callback(value);
    ipcRenderer.on("setup:progress", listener);
    return () => ipcRenderer.removeListener("setup:progress", listener);
  }
});
