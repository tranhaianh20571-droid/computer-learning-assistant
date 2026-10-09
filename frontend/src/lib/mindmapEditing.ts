import type { NodeChange } from "../excalidraw/MindMapCanvas";

export function applyMindMapChanges(kind: string, original: any, changes: NodeChange[]) {
  const content = structuredClone(original);
  for (const change of changes) {
    let node: any;
    if (change.id === "route_root" || change.id === "course_root") node = content;
    else if (kind === "route") {
      const match = /^stage_(\d+)course_(\d+)(?:unit_(\d+))?(?:lesson_(\d+))?$/.exec(change.id);
      if (match) {
        node = content.stages?.[Number(match[1])]?.courses?.[Number(match[2])];
        if (match[3] !== undefined) node = node?.units?.[Number(match[3])];
        if (match[4] !== undefined) node = node?.lessons?.[Number(match[4])];
      }
    } else {
      const match = /^unit_(\d+)(?:lesson_(\d+))?$/.exec(change.id);
      if (match) {
        node = content.units?.[Number(match[1])];
        if (match[2] !== undefined) node = node?.lessons?.[Number(match[2])];
      }
    }
    if (!node || !change.title.trim()) throw new Error("节点标题不能为空，请检查修改。");
    node.title = change.title.trim();
    if ("goal" in node || change.goal) node.goal = change.goal.trim();
  }
  return content;
}
