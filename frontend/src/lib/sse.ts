/**
 * SSE 帧解析（纯函数）：\n\n 分帧 + event:/data: 行提取 + JSON 解析。
 * 与服务端约定一致：坏 JSON 帧跳过；无 data: 行的帧跳过；
 * 事件类型取 event: 行（缺省为空串）。不打印任何内容到控制台。
 */

export type SseFrame = { event: string; data: any };

export function parseSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() || "";
  const frames: SseFrame[] = [];
  for (const part of parts) {
    const lines = part.split("\n");
    const evLine = lines.find((l) => l.startsWith("event: "));
    const dataLine = lines.find((l) => l.startsWith("data: "));
    if (!dataLine) continue;
    let data: any = null;
    try {
      data = JSON.parse(dataLine.slice(6));
    } catch {
      continue; // 坏 JSON 帧：与既有消费方行为一致，跳过
    }
    frames.push({ event: evLine?.slice(7) || "", data });
  }
  return { frames, rest };
}
