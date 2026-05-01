from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HARMONIC_BASE_URL = "https://api.harmonic.ai"
GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_INITIAL_SUBJECT = "Paradigm"
DEFAULT_INITIAL_SENDER = "dmccarthy@paradigm.xyz"
DEFAULT_INITIAL_TEMPLATE = """Hi {first_name}-

My name's Dan McCarthy - I'm a talent partner at Paradigm; we're a frontier tech investment and research firm.

I was impressed with your work on {brief_personalization}. If you're open to a quick conversation, I'd love to chat about what's going on within our portfolio and what you're paying attention to these days. Thoughts?

-Dan
"""


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ApiError(f"Missing required environment variable: {name}")
    return value


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    encoded_body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        encoded_body = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = Request(url, data=encoded_body, headers=request_headers, method=method)

    try:
        with urlopen(request) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        raise ApiError(f"{method} {url} failed", status=exc.code, body=parsed) from exc
    except URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from exc


def harmonic_headers() -> dict[str, str]:
    return {"apikey": require_env("HARMONIC_API_KEY")}


def gmail_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {require_env('GMAIL_ACCESS_TOKEN')}"}


def read_body_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def render_initial_body(*, first_name: str, brief_personalization: str) -> str:
    return DEFAULT_INITIAL_TEMPLATE.format(
        first_name=first_name.strip(),
        brief_personalization=brief_personalization.strip(),
    )


def encode_message(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message.set_content(body_text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def create_gmail_draft(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> Any:
    raw = encode_message(
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        in_reply_to=in_reply_to,
        references=references,
    )
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id

    _, payload = request_json(
        "POST",
        f"{GMAIL_BASE_URL}/drafts",
        headers=gmail_headers(),
        body={"message": message},
    )
    return payload


def send_gmail_message(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body_text: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> Any:
    raw = encode_message(
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        in_reply_to=in_reply_to,
        references=references,
    )
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id

    _, payload = request_json(
        "POST",
        f"{GMAIL_BASE_URL}/messages/send",
        headers=gmail_headers(),
        body=message,
    )
    return payload


def gmail_get_thread(thread_id: str) -> Any:
    query = urlencode(
        [
            ("format", "metadata"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "Subject"),
            ("metadataHeaders", "Message-ID"),
            ("metadataHeaders", "References"),
        ],
        doseq=True,
    )
    _, payload = request_json(
        "GET",
        f"{GMAIL_BASE_URL}/threads/{thread_id}?{query}",
        headers=gmail_headers(),
    )
    return payload


def message_headers(message: dict[str, Any]) -> dict[str, str]:
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    return {header["name"].lower(): header["value"] for header in headers}


def primary_email_from_harmonic(person: dict[str, Any]) -> str | None:
    contact = person.get("contact") or {}
    direct_emails = contact.get("emails") or []
    if direct_emails:
        return direct_emails[0]

    for experience in person.get("experience") or []:
        exp_contact = experience.get("contact") or {}
        emails = exp_contact.get("emails") or []
        if emails:
            return emails[0]
    return None


def enrich_person(linkedin_url: str) -> dict[str, Any]:
    status, payload = request_json(
        "POST",
        f"{HARMONIC_BASE_URL}/persons",
        headers=harmonic_headers(),
        body={"linkedin_url": linkedin_url},
    )
    return {
        "status": status,
        "person": payload,
        "primary_email": primary_email_from_harmonic(payload) if isinstance(payload, dict) else None,
    }


def thread_has_reply_from(thread: dict[str, Any], reply_from: str) -> bool:
    reply_from_lower = reply_from.strip().lower()
    for message in thread.get("messages") or []:
        headers = message_headers(message)
        sender = parseaddr(headers.get("from", ""))[1].lower()
        if sender == reply_from_lower:
            return True
    return False


def latest_thread_context(thread: dict[str, Any]) -> dict[str, str | None]:
    messages = thread.get("messages") or []
    if not messages:
        return {"subject": None, "message_id": None, "references": None}

    headers = message_headers(messages[-1])
    return {
        "subject": headers.get("subject"),
        "message_id": headers.get("message-id"),
        "references": headers.get("references"),
    }


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


def cmd_enrich(args: argparse.Namespace) -> int:
    print_json(enrich_person(args.linkedin_url))
    return 0


def cmd_create_draft(args: argparse.Namespace) -> int:
    payload = create_gmail_draft(
        from_email=args.from_email,
        to_email=args.to_email,
        subject=args.subject,
        body_text=read_body_file(args.body_file),
        thread_id=args.thread_id,
        in_reply_to=args.in_reply_to,
        references=args.references,
    )
    print_json(payload)
    return 0


def cmd_create_initial_draft(args: argparse.Namespace) -> int:
    payload = create_gmail_draft(
        from_email=args.from_email,
        to_email=args.to_email,
        subject=args.subject,
        body_text=render_initial_body(
            first_name=args.first_name,
            brief_personalization=args.brief_personalization,
        ),
    )
    print_json(
        {
            "subject": args.subject,
            "from": args.from_email,
            "to": args.to_email,
            "draft": payload,
        }
    )
    return 0


def cmd_check_replies(args: argparse.Namespace) -> int:
    thread = gmail_get_thread(args.thread_id)
    print_json(
        {
            "thread_id": args.thread_id,
            "reply_from": args.reply_from,
            "has_reply": thread_has_reply_from(thread, args.reply_from),
            "message_count": len(thread.get("messages") or []),
        }
    )
    return 0


def cmd_prepare_follow_up(args: argparse.Namespace) -> int:
    thread = gmail_get_thread(args.thread_id)
    if thread_has_reply_from(thread, args.reply_from):
        print_json(
            {
                "status": "reply_detected",
                "thread_id": args.thread_id,
                "reply_from": args.reply_from,
            }
        )
        return 0

    context = latest_thread_context(thread)
    if not context["subject"]:
        raise ApiError("Thread has no subject header; cannot prepare follow-up")
    if not context["message_id"]:
        raise ApiError("Thread has no Message-ID header; cannot prepare follow-up")

    references = context["references"]
    if references:
        references = f"{references} {context['message_id']}"
    else:
        references = context["message_id"]

    body_text = read_body_file(args.body_file)
    if args.send:
        payload = send_gmail_message(
            from_email=args.from_email,
            to_email=args.to_email,
            subject=context["subject"],
            body_text=body_text,
            thread_id=args.thread_id,
            in_reply_to=context["message_id"],
            references=references,
        )
        result = {
            "status": "sent",
            "thread_id": args.thread_id,
            "message": payload,
        }
    else:
        payload = create_gmail_draft(
            from_email=args.from_email,
            to_email=args.to_email,
            subject=context["subject"],
            body_text=body_text,
            thread_id=args.thread_id,
            in_reply_to=context["message_id"],
            references=references,
        )
        result = {
            "status": "draft_created",
            "thread_id": args.thread_id,
            "draft": payload,
        }

    print_json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Networking outreach helper for Harmonic and Gmail")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enrich = subparsers.add_parser("enrich", help="Enrich a person from a LinkedIn URL via Harmonic")
    enrich.add_argument("--linkedin-url", required=True)
    enrich.set_defaults(func=cmd_enrich)

    create_draft = subparsers.add_parser("create-draft", help="Create a Gmail draft")
    create_draft.add_argument("--from", dest="from_email", required=True)
    create_draft.add_argument("--to", dest="to_email", required=True)
    create_draft.add_argument("--subject", required=True)
    create_draft.add_argument("--body-file", required=True)
    create_draft.add_argument("--thread-id")
    create_draft.add_argument("--in-reply-to")
    create_draft.add_argument("--references")
    create_draft.set_defaults(func=cmd_create_draft)

    create_initial_draft = subparsers.add_parser(
        "create-initial-draft",
        help="Create the standard Paradigm first-touch Gmail draft",
    )
    create_initial_draft.add_argument("--to", dest="to_email", required=True)
    create_initial_draft.add_argument("--first-name", required=True)
    create_initial_draft.add_argument("--brief-personalization", required=True)
    create_initial_draft.add_argument("--from", dest="from_email", default=DEFAULT_INITIAL_SENDER)
    create_initial_draft.add_argument("--subject", default=DEFAULT_INITIAL_SUBJECT)
    create_initial_draft.set_defaults(func=cmd_create_initial_draft)

    check_replies = subparsers.add_parser("check-replies", help="Check whether the target has replied in a Gmail thread")
    check_replies.add_argument("--thread-id", required=True)
    check_replies.add_argument("--reply-from", required=True)
    check_replies.set_defaults(func=cmd_check_replies)

    prepare_follow_up = subparsers.add_parser(
        "prepare-follow-up",
        help="Draft or send a same-thread follow-up only when no reply exists",
    )
    prepare_follow_up.add_argument("--thread-id", required=True)
    prepare_follow_up.add_argument("--reply-from", required=True)
    prepare_follow_up.add_argument("--from", dest="from_email", required=True)
    prepare_follow_up.add_argument("--to", dest="to_email", required=True)
    prepare_follow_up.add_argument("--body-file", required=True)
    prepare_follow_up.add_argument("--send", action="store_true")
    prepare_follow_up.set_defaults(func=cmd_prepare_follow_up)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ApiError as exc:
        error = {"error": str(exc)}
        if exc.status is not None:
            error["status"] = exc.status
        if exc.body is not None:
            error["body"] = exc.body
        print_json(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
