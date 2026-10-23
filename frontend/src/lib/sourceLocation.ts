export function sourceLocation(source: any, range = true) {
  if (source?.unit === "time") {
    const format = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
    return typeof source.start_ms === "number" ?
      `${format(source.start_ms)}${range && source.end_ms > source.start_ms ? `–${format(source.end_ms)}` : ""}` : "视频";
  }
  if (source?.unit === "image") return "图片";
  if (source?.unit === "paragraph") return source.line_start ?
    `网页第 ${source.line_start}${range && source.line_end > source.line_start ? `–${source.line_end}` : ""} 行` : "网页";
  if (source?.unit === "document") return source.line_start ?
    `第 ${source.line_start}${range && source.line_end > source.line_start ? `–${source.line_end}` : ""} 行` : "";
  return source?.page_no ?
    `第 ${source.page_no}${range && source.end_page > source.page_no ? `–${source.end_page}` : ""} ${source.unit === "slide" ? "张幻灯片" : "页"}` : "";
}

export function sourceJumpUrl(source: any) {
  if (!source?.source_url) return undefined;
  try {
    const url = new URL(source.source_url);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return undefined;
    if (source.unit === "time" && typeof source.start_ms === "number") {
      url.searchParams.set("t", String(Math.max(0, Math.floor(source.start_ms / 1000))));
    }
    return url.href;
  } catch { return undefined; }
}
