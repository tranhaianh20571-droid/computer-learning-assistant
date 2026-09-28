import { useCallback, useEffect, useState } from "react";

/**
 * hash 路由：route state + hashchange 监听 + navigate()。
 * 错误/提示清理与移动端抽屉收起等副作用由调用方在导航回调里处理。
 */
export function useHashRoute() {
  const [route, setRoute] = useState<string>(() => location.hash.replace(/^#/, "") || "/home");

  useEffect(() => {
    const onHash = () => setRoute(location.hash.replace(/^#/, "") || "/home");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((path: string) => {
    location.hash = path;
    setRoute(path);
  }, []);

  return { route, navigate };
}
