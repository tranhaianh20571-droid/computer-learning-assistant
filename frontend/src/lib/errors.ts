/**
 * 错误码 → 用户可见模板文案（不显示原始异常）。
 * 与服务端受控错误码注册表对齐；未登记的码回退到 message 或通用文案。
 */

export const ERROR_TEXT: Record<string, string> = {
  TASK_STORAGE_BUSY: "服务暂时繁忙，正在准备恢复。",
  MODEL_TIMEOUT: "模型响应超时，可重试。",
  MODEL_CONNECTION_ERROR: "模型服务连接失败，可重试。",
  MODEL_RATE_LIMITED: "模型服务暂时限流，请稍后重试。",
  MODEL_QUOTA_EXCEEDED: "学习服务额度不足，请补充额度后继续。",
  MODEL_CREDENTIAL_INVALID: "模型凭据无效，请在能力设置中更新。",
  MODEL_PROVIDER_ERROR: "模型服务请求失败，可重试。",
  MODEL_RESPONSE_TOO_LARGE: "模型输出超出限制，请重试。",
  AUTH_FAILED: "邮箱或密码不正确。",
  AUTH_LOCKED: "尝试过于频繁，请稍后再试。",
  AUTH_REJECTED: "无法完成注册，请检查邮箱格式或更换邮箱。",
  EMAIL_INVALID: "邮箱格式不正确。",
  PASSWORD_TOO_SHORT: "密码至少 15 个字符。",
  TOKEN_INVALID: "链接无效或已过期。",
  SESSION_INVALID: "登录状态已失效，请重新登录。",
  ACCESS_DENIED: "没有权限执行此操作。",
  STATE_INVALID: "当前状态不允许该操作。",
  USER_NOT_FOUND: "找不到该用户。",
  PROMPT_UNAVAILABLE: "提示词暂不可用，已跳过模型调用。",
  EVENTS_EXPIRED: "进度已过期，正在重新加载。",
  TASK_NOT_FOUND: "找不到该任务。",
  TASK_EXPIRED: "生成超时未完成，可重试生成。",
  TASK_DEADLINE_EXCEEDED: "任务超过截止时间，可重试。",
  WORKER_RECOVERY_FAILED: "后台恢复失败，请稍后重试。",
  WORKER_RECOVERY_EXHAUSTED: "后台恢复次数已用完，请重新开始。",
  TASK_LEASE_EXPIRED: "后台处理已超时，可重试。",
  CONSENT_CANCELLED: "已取消外发确认，未启动生成。",
  TASK_NOT_CLAIMABLE: "任务已结束，不能再重试；请新建学习任务。",
  TASK_CANCELLED: "任务已取消。",
  LEASE_LOST: "任务租约已过期，结果已忽略。",
  config_not_found: "找不到该配置。",
  credential_invalid: "凭据无效，请检查后重试。",
  capability_test_failed: "能力测试失败，请检查服务地址与凭据。",
  capability_unavailable: "该模型不支持所需能力，已停止调用。",
  connector_offline: "连接器离线，请重新连接。",
  connector_revoked: "连接器已撤销。",
  nonce_replay: "检测到重复请求，已拒绝。",
  disclosure_expired: "外发授权已过期，请重新确认。",
  disclosure_revoked: "外发授权已撤销。",
  disclosure_required: "缺少外发授权，已阻止外发。",
  pairing_code_invalid: "配对码无效或已使用。",
  invalid_target: "连接器只允许本机回环地址。",
  admin_only: "仅管理员可执行此操作。",
  MODEL_CAPABILITY_UNAVAILABLE: "该模型不支持所需能力，已停止调用。",
  LESSON_UNLOCK_REQUIRED: "未确认大纲，暂不能开始学习。",
  DRAFT_REVISION_MISMATCH: "草稿已在其他窗口更新，已加载服务端最新版本。",
  CONTEXT_BUDGET_EXCEEDED: "上下文超出预算，请缩小范围或补充资料。",
  OUTPUT_INVALID: "模型返回的内容不符合要求，可重试生成。",
  ROUTE_STRUCTURE_INVALID: "路线结构不完整，可重试生成。",
  INVALID_OUTLINE_STRUCTURE: "大纲结构不完整，可重试生成。",
  INVALID_PREREQUISITE: "先修关系不合法，可重试生成。",
  DAG_CYCLE: "先修关系存在循环，可重试生成。",
  DISCLOSURE_REQUIRED: "需要重新准备当前模型后继续生成。",
  DISCLOSURE_EXPIRED: "本次模型使用已过期，可以使用当前模型继续生成。",
  DISCLOSURE_REVOKED: "本次模型使用已被撤销，请检查模型设置。",
  DISCLOSURE_GRANT_INVALID: "本次模型使用授权已失效，请检查模型设置。",
};

export function errText(e: unknown): string {
  const code = (e as { code?: string })?.code;
  return (code && (ERROR_TEXT[code] || ERROR_TEXT[code.toUpperCase()] || ERROR_TEXT[code.toLowerCase()])) || (e as Error)?.message || "操作失败";
}

export type Me = {
  user_id: string; email: string; is_admin: boolean; status: string; display_name: string;
  /** 访客主体（/api/auth/guest）：可使用全部学习功能，但仍显示登录入口 */
  is_guest?: boolean;
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "排队中", leased: "处理中", running: "处理中", succeeded: "已完成", partial: "部分可用",
  failed: "失败", cancelled: "已取消", expired: "已过期",
  pending_email: "待验证邮箱", pending_approval: "待审批", approved: "已通过", rejected: "已拒绝", disabled: "已停用",
};
