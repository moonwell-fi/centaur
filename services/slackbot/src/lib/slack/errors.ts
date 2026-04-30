export type SlackErrorClass =
  | "rate_limited"
  | "invalid_destination"
  | "restricted_destination"
  | "invalid_payload"
  | "duplicate_or_conflict"
  | "transient_slack_error"
  | "unknown";

export type SlackErrorClassification = {
  errorClass: SlackErrorClass;
  code?: string;
  status?: number;
  retryable: boolean;
  message: string;
};

type ErrorLike = {
  message?: unknown;
  code?: unknown;
  data?: unknown;
  response?: {
    status?: unknown;
    data?: unknown;
    headers?: unknown;
  };
};

const RATE_LIMIT_CODES = new Set(["rate_limited", "ratelimited"]);
const INVALID_DESTINATION_CODES = new Set([
  "channel_not_found",
  "duplicate_channel_not_found",
  "not_in_channel",
  "is_archived",
  "cannot_reply_to_message",
  "user_not_found",
]);
const RESTRICTED_DESTINATION_CODES = new Set([
  "restricted_action",
  "restricted_action_non_threadable_channel",
  "restricted_action_read_only_channel",
  "restricted_action_thread_locked",
  "restricted_action_thread_only_channel",
  "ekm_access_denied",
  "no_permission",
]);
const INVALID_PAYLOAD_CODES = new Set([
  "invalid_blocks",
  "invalid_blocks_format",
  "invalid_metadata_format",
  "invalid_metadata_schema",
  "invalid_post_type",
  "msg_blocks_too_long",
  "msg_too_long",
  "no_text",
]);
const TRANSIENT_CODES = new Set([
  "internal_error",
  "fatal_error",
  "request_timeout",
  "service_unavailable",
]);

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function statusValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function extractCode(error: unknown): string | undefined {
  const err = asRecord(error) as ErrorLike;
  const direct = stringValue(err.code);
  if (direct) return direct;

  const responseData = asRecord(err.response?.data);
  const responseError = responseData.error;
  if (typeof responseError === "string") return responseError;
  if (responseError && typeof responseError === "object") {
    const nested = stringValue((responseError as Record<string, unknown>).code)
      || stringValue((responseError as Record<string, unknown>).detail)
      || stringValue((responseError as Record<string, unknown>).message);
    if (nested) return nested;
  }

  const data = asRecord(err.data);
  const dataError = data.error;
  if (typeof dataError === "string") return dataError;
  return undefined;
}

function extractMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  const err = asRecord(error) as ErrorLike;
  return stringValue(err.message) || String(error);
}

export function classifySlackError(error: unknown): SlackErrorClassification {
  const message = extractMessage(error);
  const lowerMessage = message.toLowerCase();
  const code = extractCode(error);
  const normalizedCode = code?.toLowerCase();
  const status = statusValue((asRecord(error) as ErrorLike).response?.status);

  if (normalizedCode && RATE_LIMIT_CODES.has(normalizedCode)) {
    return { errorClass: "rate_limited", code, status, retryable: true, message };
  }
  if (normalizedCode && INVALID_DESTINATION_CODES.has(normalizedCode)) {
    return { errorClass: "invalid_destination", code, status, retryable: false, message };
  }
  if (normalizedCode && RESTRICTED_DESTINATION_CODES.has(normalizedCode)) {
    return { errorClass: "restricted_destination", code, status, retryable: false, message };
  }
  if (normalizedCode && INVALID_PAYLOAD_CODES.has(normalizedCode)) {
    return { errorClass: "invalid_payload", code, status, retryable: false, message };
  }
  if (normalizedCode && TRANSIENT_CODES.has(normalizedCode)) {
    return { errorClass: "transient_slack_error", code, status, retryable: true, message };
  }

  if (lowerMessage.includes("message_id was already used")
    || lowerMessage.includes("idempotency_payload_mismatch")
    || lowerMessage.includes("already used with a different payload")
    || status === 409) {
    return { errorClass: "duplicate_or_conflict", code, status, retryable: false, message };
  }
  if (lowerMessage.includes("rate_limited") || lowerMessage.includes("ratelimited")) {
    return { errorClass: "rate_limited", code, status, retryable: true, message };
  }
  if (lowerMessage.includes("channel_not_found")
    || lowerMessage.includes("not_in_channel")
    || lowerMessage.includes("user_not_found")) {
    return { errorClass: "invalid_destination", code, status, retryable: false, message };
  }
  if (lowerMessage.includes("restricted_action") || lowerMessage.includes("no_permission")) {
    return { errorClass: "restricted_destination", code, status, retryable: false, message };
  }
  if (lowerMessage.includes("invalid_blocks") || lowerMessage.includes("msg_too_long")) {
    return { errorClass: "invalid_payload", code, status, retryable: false, message };
  }
  if (status && status >= 500) {
    return { errorClass: "transient_slack_error", code, status, retryable: true, message };
  }

  return { errorClass: "unknown", code, status, retryable: true, message };
}

export class SlackApiCallError extends Error {
  readonly method: string;
  readonly code?: string;
  readonly response: unknown;

  constructor(method: string, code: string, response: unknown) {
    super(code || `Slack ${method} failed`);
    this.name = "SlackApiCallError";
    this.method = method;
    this.code = code;
    this.response = response;
  }
}
