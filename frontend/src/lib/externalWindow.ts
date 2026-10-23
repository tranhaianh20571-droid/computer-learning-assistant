import { chooseBrowser, type BrowserChoice } from "../ui/BrowserPicker";

declare global {
  interface Window {
    desktopBrowser?: {
      listBrowsers: () => Promise<BrowserChoice[]>;
      chooseExecutable: () => Promise<BrowserChoice | null>;
      openExternal: (url: string, browserId: string) => Promise<void>;
      onOpen: (callback: (url: string) => void) => () => void;
    };
  }
}

export function reserveExternalWindow() {
  if (window.desktopBrowser) return null;
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  return popup;
}

export async function openExternalPage(url: string, popup: Window | null, fallback = true) {
  if (window.desktopBrowser) {
    return chooseBrowser(url);
  }
  if (popup && !popup.closed) {
    try { popup.location.replace(url); return true; } catch { /* use the caller's fallback */ }
  }
  if (fallback) { window.location.assign(url); return true; }
  return false;
}
