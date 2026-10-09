export type ConsentService = {
  config_id: string; config_version: number; model_name?: string; protocol?: string;
  endpoint?: string; service_endpoint?: string; service_kind?: string;
};
export type ConsentChoices = { content: boolean; tts: boolean; prewarm: boolean; courses: boolean; configId: string; procedural?: boolean };
export type ConsentRequest = {
  title: string; subject: string; content: ConsentService | null; configs?: ConsentService[];
  tts?: ConsentService | null; materials?: string[]; topics?: { title: string; title_hash: string }[];
  purpose?: "start" | "prewarm" | "recover"; cover?: boolean;
};
export const emptyConsent = (): ConsentChoices => ({ content: false, tts: false, prewarm: false, courses: false, configId: "" });
export function canConfirmConsent(request: ConsentRequest, choices: ConsentChoices): boolean {
  const selected = request.configs?.length ? request.configs.find(config => config.config_id === choices.configId) : request.content;
  return Boolean(selected && choices.content && (request.purpose !== "prewarm" || choices.prewarm));
}
export function lessonConfirmation(fingerprint: string, purpose: "start" | "prewarm" | "recover", choices: ConsentChoices, conversationId?: string) {
  if (!choices.content || (purpose === "prewarm" && !choices.prewarm)) throw new Error("Explicit consent required");
  return { fingerprint, purpose, conversation_id: conversationId, content_confirmed: true,
    tts_confirmed: choices.tts, prewarm_opt_in: choices.prewarm };
}
export function coverConfirmation(preflight: { config: ConsentService | null; configs?: ConsentService[]; title_hash: string; course_topics?: { title: string; title_hash: string }[] }, choices: ConsentChoices, key: string) {
  const config = preflight.configs?.length ? preflight.configs.find(item => item.config_id === choices.configId) : preflight.config;
  if (!config || !choices.content) throw new Error("Explicit consent required");
  return { external_authorized: true, config_id: config.config_id, config_version: config.config_version,
    title_hash: preflight.title_hash, authorization_scope: "exact_target",
    course_topic_hashes: [], idempotency_key: key };
}
