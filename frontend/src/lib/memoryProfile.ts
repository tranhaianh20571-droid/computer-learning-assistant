export type MemoryProfileSection = { title: string; paragraphs: string[] };

/** Only recognize headings outside fenced blocks; all body text stays literal. */
export function memoryProfileSections(content: string): MemoryProfileSection[] {
  const sections: MemoryProfileSection[] = [];
  let title = "";
  let lines: string[] = [];
  let fence = "";
  function flush() {
    const body = lines.join("\n").trim();
    if (title || body) sections.push({ title, paragraphs: body ? body.split(/\n\s*\n/) : [] });
    lines = [];
  }
  for (const line of content.replace(/\r\n?/g, "\n").split("\n")) {
    const marker = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = "";
      lines.push(line);
      continue;
    }
    const heading = !fence && line.match(/^\s{0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$/);
    if (heading) {
      flush();
      title = heading[1];
    } else lines.push(line);
  }
  flush();
  return sections;
}
