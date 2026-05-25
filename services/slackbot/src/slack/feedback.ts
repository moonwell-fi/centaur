export type SlackFeedbackCommandPayload = {
  command?: string
  text?: string
  user_id?: string
  user_name?: string
  channel_id?: string
  channel_name?: string
  team_id?: string
  team_domain?: string
  thread_ts?: string
}

export type SlackFeedbackTranscriptMessage = {
  ts: string
  user: string | null
  bot_id: string | null
  text: string
}

export function buildSlackFeedbackWebhookPayload(
  payload: SlackFeedbackCommandPayload,
  text: string,
  transcript: SlackFeedbackTranscriptMessage[],
  env: Record<string, string | undefined> = process.env
) {
  return {
    type: 'centaur.slack_feedback',
    version: 1,
    feedback: {
      command: payload.command,
      text,
      submitted_by: payload.user_id ? `<@${payload.user_id}>` : (payload.user_name ?? null),
      submitted_at: new Date().toISOString()
    },
    slack: {
      team_id: payload.team_id ?? null,
      team_domain: payload.team_domain ?? null,
      channel_id: payload.channel_id ?? null,
      channel_name: payload.channel_name ?? null,
      thread_ts: payload.thread_ts ?? null,
      permalink: feedbackPermalink(payload)
    },
    deployment: {
      commit: env.COMMIT_SHA ?? null,
      image: env.CENTAUR_IMAGE ?? env.IMAGE ?? null,
      image_tag: env.CENTAUR_IMAGE_TAG ?? env.IMAGE_TAG ?? null,
      release: env.CENTAUR_RELEASE ?? env.RELEASE_NAME ?? null,
      namespace: env.CENTAUR_NAMESPACE ?? env.NAMESPACE ?? null,
      hostname: env.HOSTNAME ?? null
    },
    transcript
  }
}

export function feedbackPermalink(payload: SlackFeedbackCommandPayload): string | null {
  if (!payload.channel_id || !payload.thread_ts) return null
  return `https://slack.com/archives/${payload.channel_id}/p${payload.thread_ts.replace('.', '')}`
}
