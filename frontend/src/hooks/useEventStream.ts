import { useCallback, useEffect, useRef } from "react";
import { parseSseFrames, type SseFrame } from "../lib/sse";

/**
 * 任务事件流 hook：管理 AbortController 生命周期（卸载自动断流、
 * 新连接前断开旧流），封装 fetch + getReader + 分帧。
 * - open 接收 signal，调用方负责传给 fetch（如 api.taskSse(id, undefined, signal)）；
 * - 410 events_expired 时回调 onExpired（REST 全量重建），不视为错误；
 * - onFrame 每个解析后的 SSE 帧回调一次。
 */
export function useEventStream() {
  const ctrlRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    ctrlRef.current?.abort();
  }, []);

  useEffect(() => () => ctrlRef.current?.abort(), []);

  const runStream = useCallback(
    async (
      open: (signal: AbortSignal) => Promise<Response>,
      onFrame: (frame: SseFrame) => void,
      opts: { onExpired?: () => Promise<void> } = {},
    ) => {
      abort();
      const ctrl = new AbortController();
      ctrlRef.current = ctrl;
      try {
        const res = await open(ctrl.signal);
        if (!res.ok || !res.body) {
          if (res.status === 410 && opts.onExpired) {
            await opts.onExpired();
            return;
          }
          throw new Error("SSE failed");
        }
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done || ctrl.signal.aborted) break;
          buf += dec.decode(value, { stream: true });
          const parsed = parseSseFrames(buf);
          buf = parsed.rest;
          for (const frame of parsed.frames) onFrame(frame);
        }
      } finally {
        if (ctrlRef.current === ctrl) ctrlRef.current = null;
      }
    },
    [abort],
  );

  return { runStream, abort };
}
