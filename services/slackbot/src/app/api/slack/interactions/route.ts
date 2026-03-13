import { NextRequest, NextResponse } from "next/server";
import { log } from "@/lib/logger";
import { verifySlackSignature } from "@/lib/bot/slack-client";
import { API_URL, resilientFetch } from "@/lib/bot/api-client";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const SIGNING_SECRET = process.env.SLACK_SIGNING_SECRET || "";

type BlockActionPayload = {
  type: "block_actions";
  user: { id: string; name: string; username?: string };
  channel?: { id: string };
  message?: { ts: string; thread_ts?: string };
  actions?: Array<{ action_id: string; value?: string }>;
  response_url?: string;
};

export async function POST(request: NextRequest) {
  const rawBody = await request.text();
  const signature = request.headers.get("x-slack-signature") || "";
  const timestamp = request.headers.get("x-slack-request-timestamp") || "";

  const { valid, reason } = verifySlackSignature(
    SIGNING_SECRET,
    signature,
    timestamp,
    rawBody,
  );
  if (!valid) {
    log.error("slack_interactions_rejected", { reason });
    return NextResponse.json({ error: "Invalid Slack signature" }, { status: 401 });
  }

  const params = new URLSearchParams(rawBody);
  const payloadStr = params.get("payload");
  if (!payloadStr) {
    return NextResponse.json({ error: "Missing payload" }, { status: 400 });
  }

  let payload: BlockActionPayload;
  try {
    payload = JSON.parse(payloadStr) as BlockActionPayload;
  } catch {
    return NextResponse.json({ error: "Invalid payload JSON" }, { status: 400 });
  }

  if (payload.type !== "block_actions" || !payload.actions?.length) {
    return NextResponse.json({ ok: true });
  }

  const action = payload.actions[0];
  const userId = payload.user?.id || "";
  const userName = payload.user?.name || payload.user?.username || "Someone";

  if (action.action_id === "whosaidit_pick") {
    const value = action.value || "";
    const [gameId, pickUserId] = value.split("|");
    if (!gameId || !pickUserId) {
      return NextResponse.json({
        response_type: "ephemeral",
        text: "Invalid game state. Please try again.",
      });
    }

    try {
      const res = await resilientFetch(`${API_URL}/game/answer`, {
        method: "POST",
        body: JSON.stringify({
          game_id: gameId,
          user_id: userId,
          user_name: userName,
          pick_user_id: pickUserId,
        }),
      });
      if (!res.ok) {
        log.warn("game_answer_failed", { status: res.status, game_id: gameId });
      }
    } catch (err) {
      log.error("game_answer_error", {
        error: err instanceof Error ? err.message : String(err),
        game_id: gameId,
      });
    }

    const displayName =
      (action as { text?: { text?: string } }).text?.text || "your choice";

    return NextResponse.json({
      response_type: "ephemeral",
      text: `✅ You picked: ${displayName}\nWaiting for others...`,
    });
  }

  if (action.action_id === "whosaidit_play_again") {
    const channelId = payload.channel?.id;
    const threadTs = payload.message?.thread_ts || payload.message?.ts;
    if (!channelId || !threadTs) {
      return NextResponse.json({
        response_type: "ephemeral",
        text: "Could not start a new game. Missing channel context.",
      });
    }

    try {
      await resilientFetch(`${API_URL}/game/start`, {
        method: "POST",
        body: JSON.stringify({
          channel_id: channelId,
          thread_ts: threadTs,
        }),
      });
    } catch (err) {
      log.error("game_play_again_error", {
        error: err instanceof Error ? err.message : String(err),
      });
      return NextResponse.json({
        response_type: "ephemeral",
        text: "Failed to start a new game. Try @centaur play whosaidit",
      });
    }

    return NextResponse.json({
      response_type: "ephemeral",
      text: "🎮 Starting a new game...",
    });
  }

  return NextResponse.json({ ok: true });
}
