import MindMapCanvas from "./MindMapCanvas";

/** Mind-map browsing preserves the scene protocol and uses system print fonts. */
export default function ReadonlyCanvas({ elements, height = 460,
  ariaLabel = "思维导图，可拖动与缩放" }: {
  elements: Record<string, any>[]; height?: number | "full"; ariaLabel?: string;
}) {
  return <MindMapCanvas elements={elements} height={height} ariaLabel={ariaLabel} readOnly />;
}
