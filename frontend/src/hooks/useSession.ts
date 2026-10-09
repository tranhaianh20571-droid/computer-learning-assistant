import { useEffect, useState } from "react";
import { api, setSessionToken } from "../api";
import type { Me } from "../lib/errors";

const AUTH_DISABLED = import.meta.env.DEV && import.meta.env.VITE_DISABLE_AUTH === "1";

/** 未登录时以访客身份继续：先于其他请求拿到访客 Cookie，避免并发首请求各自生成访客身份。 */
export async function loadGuest(): Promise<Me | null> {
  try {
    return { ...(await api.guest()), is_guest: true };
  } catch {
    return null;
  }
}

/**
 * 会话恢复：启动时无条件调 me()——服务端 14 天持久 Cookie 随请求自动携带，
 * 刷新/重开浏览器后据此恢复登录态；401 时回落为访客主体（内存 token 仅为 Bearer 备用）。
 */
export function useSession() {
  const [me, setMe] = useState<Me | null>(null);
  const [booted, setBooted] = useState(false);

  useEffect(() => {
    if (AUTH_DISABLED) {
      // 调试旁路仅伪造前端展示；后端仍按访客解析，先引导访客 Cookie 再渲染，保证身份稳定
      (async () => {
        await loadGuest();
        setMe({
          user_id: "debug-user",
          email: "debug@localhost",
          is_admin: false,
          status: "approved",
          display_name: "调试用户",
        });
        setBooted(true);
      })();
      return;
    }
    (async () => {
      try {
        setMe(await api.me());
      } catch {
        setSessionToken(null);
        setMe(await loadGuest());
      }
      setBooted(true);
    })();
  }, []);

  return { me, setMe, booted };
}
