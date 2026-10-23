import { useEffect, useRef } from "react";
import { characterApi } from "../api";

/**
 * Reads only this signed-in account's notifications. Reception itself runs in
 * the backend and continues when this page unmounts or the user signs out.
 */
export function useMarketImportPoller(onImported: (count: number) => void) {
  const live = useRef(true);
  const seen = useRef(new Set<string>());
  const onImportedRef = useRef(onImported);
  onImportedRef.current = onImported;

  useEffect(() => {
    live.current = true;
    let timer = 0;
    const tick = async () => {
      if (!live.current) return;
      try {
        const result: any = await characterApi.marketDrainImports();
        if (!live.current) return;
        const cards = result?.cards || [];
        const fresh = cards.filter((card: any) => card?.job_id && !seen.current.has(card.job_id));
        for (const card of fresh) seen.current.add(card.job_id);
        if (fresh.length) onImportedRef.current(fresh.length);
      } catch { /* 未绑定或市集暂不可达时静默重试 */ }
      if (live.current) timer = window.setTimeout(tick, 12_000);
    };
    timer = window.setTimeout(tick, 4_000);
    return () => {
      live.current = false;
      window.clearTimeout(timer);
    };
  }, []);
}
