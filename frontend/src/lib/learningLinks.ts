export function courseLink(courseId: string, routeId?: string, versionId?: string) {
  const query = new URLSearchParams();
  if (routeId) query.set("route_id", routeId);
  if (versionId) query.set("route_version_id", versionId);
  return `/course/${courseId}${query.size ? `?${query}` : ""}`;
}

export function routeLink(routeId: string, versionId?: string) {
  return `/route/${routeId}${versionId ? `?version_id=${encodeURIComponent(versionId)}` : ""}`;
}

export function planningMatchLink(item: { kind: string; id: string }) {
  return item.kind === "route" ? routeLink(item.id) : courseLink(item.id);
}
