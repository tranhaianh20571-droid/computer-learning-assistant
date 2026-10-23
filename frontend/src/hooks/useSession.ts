import { useEffect, useState } from "react";
import { api, characterApi, setSessionToken } from "../api";
import type { Me } from "../lib/errors";

/** Restore a valid server session; authentication failures never create guests. */
export function useSession() {
  const [me, setMe] = useState<Me | null>(null);
  const [booted, setBooted] = useState(false);
  const owner = me?.user_id;

  useEffect(() => {
    if (!owner) return;
    let alive = true;
    let pending = false;
    const sync = async () => {
      if (pending) return;
      pending = true;
      try {
        const profile = await characterApi.marketProfile();
        if (alive) setMe(current => current?.user_id === owner ? { ...current, display_name: profile.display_name } : current);
      } catch { /* Offline installs retain the last confirmed profile. */ }
      finally { pending = false; }
    };
    void sync();
    window.addEventListener("focus", sync);
    return () => { alive = false; window.removeEventListener("focus", sync); };
  }, [owner]);

  useEffect(() => {
    (async () => {
      try {
        setMe(await api.me());
      } catch {
        setSessionToken(null);
        setMe(null);
      }
      setBooted(true);
    })();
  }, []);

  useEffect(() => {
    const expired = () => { setSessionToken(null); setMe(null); };
    window.addEventListener("session-expired", expired);
    return () => window.removeEventListener("session-expired", expired);
  }, []);

  return { me, setMe, booted };
}
