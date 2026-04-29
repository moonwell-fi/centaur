import { after } from "next/server";
import { NextRequest, NextResponse } from "next/server";
import { log } from "@/lib/logger";
import { ensureBotReady, getSlackBootstrapState } from "@/lib/bot/setup";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

/**
 * Slack webhook handler -- verifies and routes Events API payloads through
 * the Bolt-backed Slack app adapter.
 */
export async function POST(request: NextRequest) {
  const requestId = request.headers.get("x-slack-request-id") || "";
  const retryNum = request.headers.get("x-slack-retry-num") || "";

  log.info("webhook_received", { request_id: requestId, retry_num: retryNum });

  const bootstrap = getSlackBootstrapState();
  if (!bootstrap.ready) {
    log.error("slack_webhook_unavailable", {
      request_id: requestId,
      retry_num: retryNum,
      missing_env_keys: bootstrap.missingEnvKeys,
      invalid_env_keys: bootstrap.invalidEnvKeys,
    });
    return NextResponse.json(
      {
        error: "slack webhook unavailable",
        missing_env_keys: bootstrap.missingEnvKeys,
        invalid_env_keys: bootstrap.invalidEnvKeys,
      },
      { status: 503 },
    );
  }

  let app;
  try {
    app = await ensureBotReady();
  } catch (error) {
    log.error("slack_webhook_init_failed", {
      request_id: requestId,
      retry_num: retryNum,
      error: error instanceof Error ? error.message : String(error),
    });
    return NextResponse.json({ error: "slack webhook unavailable" }, { status: 503 });
  }

  log.info("webhook_dispatched", { request_id: requestId, retry_num: retryNum });

  try {
    return await app.handleRequest(request, {
      waitUntil: (task) => after(() => task),
    });
  } catch (error) {
    log.error("slack_events_handler_failed", {
      request_id: requestId,
      retry_num: retryNum,
      error: error instanceof Error ? error.message : String(error),
    });
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
