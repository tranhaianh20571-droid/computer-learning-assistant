const { contextBridge, ipcRenderer } = require("electron");
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
