# Networking Outreach Architecture

This flow is possible, but it works best as two layers:

- A skill for enrichment, research, personalization, and draft creation.
- A scheduler or durable workflow for delayed reply checks and follow-ups.

## Why Split It This Way

The skill is a good fit for high-context reasoning work:

- turn a LinkedIn URL into a person record
- inspect research output and recent work
- personalize an email from a template
- create the first Gmail draft

The delayed follow-up is operational rather than reasoning-heavy:

- wait `N` days
- check the Gmail thread
- do nothing if the recipient replied
- otherwise create or send the follow-up in the same thread

## External API Facts

Harmonic:

- Official docs say `POST https://api.harmonic.ai/persons` accepts `linkedin_url` or `email`.
- Authentication uses the `apikey` header or query parameter.
- Enrichment can return `200`, `201`, or `404` depending on freshness and whether enrichment is still being queued.
- Person responses can include `contact.emails` and other background useful for personalization.

Gmail:

- Official docs support `users.drafts.create` for draft creation.
- Follow-ups can stay in the same thread when the outgoing message includes the target `threadId`, matching subject, and RFC-compliant `In-Reply-To` and `References` headers.
- Thread inspection works through `users.threads.get`.

## Recommended State To Persist

At minimum, persist:

- `linkedin_url`
- `recipient_email`
- `thread_id`
- `from_email` (default `dmccarthy@paradigm.xyz` for the standard first email)
- `follow_up_delay_days`
- `sent_at`
- the final initial email subject (default `Paradigm`)

Nice to have:

- Harmonic person JSON snapshot
- research notes used for personalization
- the exact initial body and follow-up body

## Credential Assumptions

The included script assumes:

- `HARMONIC_API_KEY` is available in the environment.
- `GMAIL_ACCESS_TOKEN` is a valid OAuth bearer token with Gmail access for the sending mailbox.

If this is meant to run unattended in production, replace short-lived access-token handling with a proper OAuth refresh-token flow or domain-wide delegated Google Workspace setup.

## Safety Defaults

- Initial email is drafted, not sent.
- Follow-up is drafted by default.
- Automatic sending is opt-in.
- No follow-up is sent when a reply from the target already exists in the thread.
