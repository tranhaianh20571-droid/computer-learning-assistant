import { useCallback, useEffect, useState } from "react";

const ROUTE_ALIASES: Record<string, string> = {
  "/settings": "/capabilities",
  "/settings/models": "/capabilities",
  "/settings/account": "/capabilities",
};

const STATIC_ROUTES = new Set([
  "/home", "/login", "/register", "/verify", "/reset", "/chat", "/planning",
  "/courses", "/materials", "/tasks", "/admin", "/canvas", "/capabilities",
  "/cards", "/card-market", "/memory", "/market-receive", "/review",
]);

/** Keep stale demo links and malformed hashes from rendering an empty shell. */
function normalizeRoute(raw: string) {
  const route = raw || "/home";
  if (ROUTE_ALIASES[route]) return ROUTE_ALIASES[route];
  const path = route.split("?")[0];
  if (STATIC_ROUTES.has(path)) return ["/cards", "/card-market", "/capabilities", "/review"].includes(path) ? route : path;
  if (/^\/(?:route|course|lecture|card|knowledge)\/[^/?]+$/.test(path)) return route;
  return "/home";
}

/**
 * hash 路由：route state + hashchange 监听 + navigate()。
 * 错误/提示清理与移动端抽屉收起等副作用由调用方在导航回调里处理。
 */
export function useHashRoute() {
  const [route, setRoute] = useState<string>(() => normalizeRoute(location.hash.replace(/^#/, "")));

  useEffect(() => {
    const onHash = () => {
      const next = normalizeRoute(location.hash.replace(/^#/, ""));
      setRoute(next);
      if (location.hash.replace(/^#/, "") !== next) location.hash = next;
    };
    onHash();
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((path: string) => {
    const next = normalizeRoute(path);
    location.hash = next;
    setRoute(next);
  }, []);

  return { route, navigate };
}
