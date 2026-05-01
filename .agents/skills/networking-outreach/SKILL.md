---
name: networking-outreach
description: "Builds personalized outreach from a LinkedIn profile by enriching contact data with Harmonic, researching the target's work, drafting Gmail messages, and preparing follow-ups in the same Gmail thread. Use when setting up semi-automated outbound or drip email workflows."
---

# Networking Outreach

Uses Harmonic for person enrichment, public-web research for personalization, and Gmail for draft creation or same-thread follow-up.

## Default First Email

Unless the user says otherwise, the initial outreach email uses:

- Subject: `Paradigm`
- Sender: `dmccarthy@paradigm.xyz`
- Body template:

```text
Hi [FIRST NAME]-

My name's Dan McCarthy - I'm a talent partner at Paradigm; we're a frontier tech investment and research firm.

I was impressed with your work on [BRIEF PERSONALIZATION]. If you're open to a quick conversation, I'd love to chat about what's going on within our portfolio and what you're paying attention to these days. Thoughts?

-Dan
```

## Use This Skill When

- The user wants outreach generated from a LinkedIn profile.
- The user wants a reusable flow for personalized cold email.
- The user wants Gmail drafts created automatically.
- The user wants a follow-up message only if no reply has landed after a delay.

## Inputs To Collect

- `linkedin_url`
- `follow_up_template`
- `follow_up_delay_days`
- Any personalization constraints such as tone, length, CTA, or forbidden claims

If the user wants the default first email above, do not ask for a separate initial template.

## Workflow

1. Enrich the person in Harmonic.
Run `uv run .agents/skills/networking-outreach/scripts/networking_outreach.py enrich --linkedin-url <url>`.

2. Review relevant work.
Use `web_search` and `read_web_page` to pull 2-4 concrete details worth referencing. Favor official sites, publication pages, lab pages, GitHub, personal websites, and recent talks over generic summaries.

3. Personalize the outreach.
For the first email, keep the default template fixed unless the user provided a replacement. Replace only `[FIRST NAME]` and `[BRIEF PERSONALIZATION]`. The personalization should name one concrete project, paper, product area, or recent technical contribution. Do not invent shared context, outcomes, or mutual connections.

4. Create the first Gmail draft.
Run:
`uv run .agents/skills/networking-outreach/scripts/networking_outreach.py create-initial-draft --to <recipient_email> --first-name <first_name> --brief-personalization <personalization>`

This command defaults to subject `Paradigm` and sender `dmccarthy@paradigm.xyz`.

5. Track the sent thread.
Once the user sends the draft, keep the returned Gmail `threadId` and the recipient email. Those are the minimum pieces needed for reply detection.

6. Prepare the follow-up.
After the agreed delay, run:
`uv run .agents/skills/networking-outreach/scripts/networking_outreach.py prepare-follow-up --thread-id <thread_id> --reply-from <recipient_email> --to <recipient_email> --from <from_email> --body-file <path>`

7. Only auto-send with explicit approval.
By default, create a draft follow-up. Use `--send` only if the user explicitly wants unattended follow-ups and has approved that behavior.

## Guardrails

- Default to draft creation, not automatic sending.
- Do not send a follow-up if the thread already contains a reply from the target.
- Keep personalization grounded in the reviewed research.
- If Harmonic enrichment returns `201` or `404`, explain that enrichment may still be running and the email may not be available yet.
- If Harmonic returns no email, stop and say so plainly instead of guessing.

## Credentials

- `HARMONIC_API_KEY`
- `GMAIL_ACCESS_TOKEN`

## Notes

- Harmonic supports enriching a person directly from `linkedin_url`.
- Gmail drafts and same-thread follow-ups require a valid Gmail OAuth access token.
- Threaded follow-ups must include the Gmail `threadId` plus RFC-compliant `In-Reply-To` and `References` headers.

## Reference

- See `reference/architecture.md` for the system shape and credential assumptions.
