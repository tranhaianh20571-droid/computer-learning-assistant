import { useEffect, useState } from "react";
import { api, setSessionToken } from "../api";
import type { Me } from "../lib/errors";

/**
 * 会话恢复：启动时无条件调 me()——服务端 14 天持久 Cookie 随请求自动携带，
 * 刷新/重开浏览器后据此恢复登录态；401 才落到登录页（内存 token 仅为 Bearer 备用）。
 */
export function useSession() {
  const [me, setMe] = useState<Me | null>(null);
  const [booted, setBooted] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        setMe(await api.me());
      } catch {
        setSessionToken(null);
      }
      setBooted(true);
    })();
  }, []);

  return { me, setMe, booted };
}
