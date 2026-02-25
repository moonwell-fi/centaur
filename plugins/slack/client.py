"""Slack API client - bot token only (no user token required)."""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# Cache for channel list to avoid repeated API calls

class SlackClient:
    """Slack API client — bot token only (no user token required)."""

    # Cache settings
    _CACHE_DIR = Path.home() / ".cache" / "tempo-slack"
    _CHANNEL_CACHE_FILE = _CACHE_DIR / "channels.json"
    _USER_CACHE_FILE = _CACHE_DIR / "users.json"
    _CHANNEL_CACHE_TTL = 300  # 5 minutes
    _USER_CACHE_TTL = 600  # 10 minutes

    def __init__(self, bot_token: str | None = None):
        token = bot_token or os.getenv("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "SLACK_BOT_TOKEN not set.\n"
                "Get one at https://api.slack.com/apps → OAuth & Permissions → Bot User OAuth Token"
            )
        self._client = WebClient(token=token)
        self._user_cache: dict[str, str] = {}



    def _retry_on_ratelimit(self, func, *args, max_retries: int = 3, **kwargs):
        """Retry a function on rate limit errors with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except SlackApiError as e:
                if e.response.get("error") == "ratelimited":
                    retry_after = int(e.response.headers.get("Retry-After", 5))
                    if attempt < max_retries - 1:
                        time.sleep(retry_after)
                        continue
                raise
        raise RuntimeError("Max retries exceeded")


    def _resolve_mentions(self, text: str, user_cache: dict[str, str]) -> str:
        """Replace <@USER_ID> mentions with @username."""

        def replace_mention(match: re.Match) -> str:
            user_id = match.group(1)
            if user_id in user_cache:
                return f"@{user_cache[user_id]}"
            try:
                info = self._client.users_info(user=user_id)
                name = info.get("user", {}).get("name", user_id)
                user_cache[user_id] = name
                return f"@{name}"
            except SlackApiError:
                user_cache[user_id] = user_id
                return f"@{user_id}"

        return re.sub(r"<@([A-Z0-9]+)>", replace_mention, text)


    def _load_channel_cache(self) -> tuple[list[dict], float] | None:
        """Load cached channel list if valid."""
        try:
            if self._CHANNEL_CACHE_FILE.exists():
                data = json.loads(self._CHANNEL_CACHE_FILE.read_text())
                cached_at = data.get("cached_at", 0)
                if time.time() - cached_at < self._CHANNEL_CACHE_TTL:
                    return data.get("channels", []), cached_at
        except Exception:
            pass
        return None


    def _save_channel_cache(self, channels: list[dict]) -> None:
        """Save channel list to cache."""
        try:
            self._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._CHANNEL_CACHE_FILE.write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "channels": channels,
                    }
                )
            )
        except Exception:
            pass


    def _load_user_cache(self) -> dict[str, str] | None:
        """Load cached user list if valid."""
        try:
            if self._USER_CACHE_FILE.exists():
                data = json.loads(self._USER_CACHE_FILE.read_text())
                cached_at = data.get("cached_at", 0)
                if time.time() - cached_at < self._USER_CACHE_TTL:
                    return data.get("users", {})
        except Exception:
            pass
        return None


    def _save_user_cache(self, users: dict[str, str]) -> None:
        """Save user mapping to cache."""
        try:
            self._CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._USER_CACHE_FILE.write_text(
                json.dumps(
                    {
                        "cached_at": time.time(),
                        "users": users,
                    }
                )
            )
        except Exception:
            pass


    def _get_user_cache(self) -> dict[str, str]:
        """Get user ID -> name mapping, using cache when possible."""
        cached = self._load_user_cache()
        if cached:
            return cached

        user_cache: dict[str, str] = {}
        try:
            users_response = self._retry_on_ratelimit(self._client.users_list, limit=1000)
            for user in users_response.get("members", []):
                user_cache[user.get("id", "")] = user.get("name", "")
            self._save_user_cache(user_cache)
        except SlackApiError:
            pass
        return user_cache


    def list_bot_channels(self, 
        include_private: bool = True, limit: int = 500, force_refresh: bool = False
    ) -> list[dict]:
        """List channels the bot is a member of.

        Args:
            include_private: Include private channels
            limit: Maximum channels to return
            force_refresh: Ignore cache and fetch fresh data

        Returns:
            List of channel dicts with id, name, is_private
        """
        # Check cache first
        if not force_refresh:
            cached = self._load_channel_cache()
            if cached:
                channels, _ = cached
                return channels[:limit]

        client = self._client
        channels = []
        cursor = None
        types = "public_channel,private_channel" if include_private else "public_channel"

        while True:
            try:
                response = self._retry_on_ratelimit(
                    self._client.conversations_list,
                    types=types,
                    limit=min(limit - len(channels), 200),
                    cursor=cursor,
                    exclude_archived=True,
                )
            except SlackApiError as e:
                raise RuntimeError(f"Slack API error: {e.response['error']}")

            for channel in response.get("channels", []):
                if channel.get("is_member", False):
                    channels.append(
                        {
                            "id": channel.get("id", ""),
                            "name": channel.get("name", ""),
                            "purpose": channel.get("purpose", {}).get("value", ""),
                            "topic": channel.get("topic", {}).get("value", ""),
                            "member_count": channel.get("num_members", 0),
                            "is_private": channel.get("is_private", False),
                        }
                    )

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor or len(channels) >= limit:
                break

        result = sorted(channels, key=lambda x: x["name"])
        self._save_channel_cache(result)
        return result


    def _fetch_channel_history(self, 
        client: WebClient,
        channel_id: str,
        channel_name: str,
        limit: int,
        user_cache: dict[str, str],
    ) -> list[dict]:
        """Fetch history for a single channel (used by search)."""
        try:
            response = self._retry_on_ratelimit(
                self._client.conversations_history,
                channel=channel_id,
                limit=limit,
            )
        except SlackApiError:
            return []

        messages = []
        for msg in response.get("messages", []):
            user_id = msg.get("user", "")
            username = user_cache.get(user_id, user_id)
            text = msg.get("text", "")
            ts = msg.get("ts", "")

            messages.append(
                {
                    "channel": channel_name,
                    "channel_id": channel_id,
                    "user": username,
                    "user_id": user_id,
                    "text": text,
                    "timestamp": ts,
                    "permalink": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                    "thread_ts": msg.get("thread_ts"),
                    "reply_count": msg.get("reply_count", 0),
                }
            )

        return messages


    def _score_match(self, query_terms: list[str], text: str) -> float:
        """Score how well text matches query terms. Higher = better match."""
        text_lower = text.lower()
        score = 0.0

        # Exact phrase match (highest score)
        full_query = " ".join(query_terms)
        if full_query in text_lower:
            score += 10.0

        # Individual term matches
        for term in query_terms:
            if term in text_lower:
                score += 1.0
                # Bonus for word boundary matches
                if f" {term} " in f" {text_lower} ":
                    score += 0.5

        # Penalty for very long messages (likely less relevant)
        if len(text) > 500:
            score *= 0.8

        return score


    def search_messages(self, 
        query: str,
        max_results: int = 20,
        channels: list[str] | None = None,
        from_user: str | None = None,
        messages_per_channel: int = 200,
    ) -> list[dict]:
        """Search messages across bot-accessible channels using local filtering.

        Searches all channels the bot is a member of. For true cross-workspace search,
        a user token with search:read scope would be required.

        Args:
            query: Text to search for (case-insensitive, supports multiple terms)
            max_results: Maximum results to return
            channels: Optional list of channel names to search (default: all bot channels)
            from_user: Optional username to filter by
            messages_per_channel: Messages to fetch per channel for searching

        Returns:
            List of matching message dicts, sorted by relevance
        """
        client = self._client

        bot_channels = self.list_bot_channels()
        if channels:
            channel_names = {c.lstrip("#").lower() for c in channels}
            bot_channels = [c for c in bot_channels if c["name"].lower() in channel_names]

        if not bot_channels:
            return []

        # Use cached user lookup
        user_cache = self._get_user_cache()

        # Fetch messages from all channels in parallel
        all_messages = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {
                executor.submit(
                    self._fetch_channel_history,
                    client,
                    ch["id"],
                    ch["name"],
                    messages_per_channel,
                    user_cache,
                ): ch
                for ch in bot_channels
            }

            for future in as_completed(futures):
                try:
                    messages = future.result()
                    all_messages.extend(messages)
                except Exception:
                    pass

        # Parse query into terms for smarter matching
        query_lower = query.lower()
        query_terms = [t.strip() for t in query_lower.split() if t.strip()]

        # Score and filter messages
        scored_results = []

        for msg in all_messages:
            text_lower = msg["text"].lower()

            # Must contain at least one query term
            if not any(term in text_lower for term in query_terms):
                continue

            if from_user:
                username = user_cache.get(msg["user_id"], msg["user_id"])
                if from_user.lower().lstrip("@") != username.lower():
                    continue

            # Calculate relevance score
            score = self._score_match(query_terms, msg["text"])

            msg["user"] = user_cache.get(msg["user_id"], msg["user_id"])
            msg["text"] = self._resolve_mentions(msg["text"], user_cache)
            msg["_score"] = score
            scored_results.append(msg)

        # Sort by score (relevance) first, then by timestamp
        scored_results.sort(key=lambda x: (-x["_score"], -float(x["timestamp"])))

        # Remove internal score before returning
        for msg in scored_results:
            del msg["_score"]

        return scored_results[:max_results]


    def get_channel_history(self, channel: str, limit: int = 50) -> list[dict]:
        """Get recent messages from a channel.

        Args:
            channel: Channel name (without #) or channel ID
            limit: Maximum number of messages to return

        Returns:
            List of message dicts
        """
        client = self._client
        user_cache: dict[str, str] = {}

        def get_username(user_id: str) -> str:
            if not user_id:
                return "unknown"
            if user_id in user_cache:
                return user_cache[user_id]
            try:
                info = self._client.users_info(user=user_id)
                name = info.get("user", {}).get("name", user_id)
                user_cache[user_id] = name
                return name
            except SlackApiError:
                user_cache[user_id] = user_id
                return user_id

        channel_id = channel
        if not channel.startswith("C") and not channel.startswith("G"):
            channels = self.list_bot_channels()
            for ch in channels:
                if ch["name"] == channel or ch["name"] == channel.lstrip("#"):
                    channel_id = ch["id"]
                    break
            else:
                raise RuntimeError(f"Channel '{channel}' not found or bot not a member")

        try:
            response = self._client.conversations_history(channel=channel_id, limit=limit)
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")

        messages = []
        for msg in response.get("messages", []):
            user_id = msg.get("user", "")
            text = self._resolve_mentions(msg.get("text", ""), user_cache)
            ts = msg.get("ts", "")

            messages.append(
                {
                    "user": get_username(user_id),
                    "text": text,
                    "timestamp": ts,
                    "permalink": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                    "channel_id": channel_id,
                    "thread_ts": msg.get("thread_ts"),
                    "reply_count": msg.get("reply_count", 0),
                }
            )

        return messages


    def get_thread_replies(self, channel_id: str, thread_ts: str, limit: int = 100) -> list[dict]:
        """Get all replies in a thread."""
        client = self._client
        user_cache = self._get_user_cache()

        def get_username(user_id: str) -> str:
            if user_id in user_cache:
                return user_cache[user_id]
            try:
                info = self._retry_on_ratelimit(self._client.users_info, user=user_id)
                name = info.get("user", {}).get("name", user_id)
                user_cache[user_id] = name
                return name
            except SlackApiError:
                return user_id

        try:
            response = self._retry_on_ratelimit(
                self._client.conversations_replies,
                channel=channel_id,
                ts=thread_ts,
                limit=limit,
                inclusive=True,
            )
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")

        messages = []
        for msg in response.get("messages", []):
            user_id = msg.get("user", "")
            text = self._resolve_mentions(msg.get("text", ""), user_cache)
            messages.append(
                {
                    "user": get_username(user_id),
                    "text": text,
                    "timestamp": msg.get("ts", ""),
                }
            )

        return messages


    def list_channels(self, include_private: bool = False, limit: int = 200) -> list[dict]:
        """List all Slack channels (not just bot member channels)."""
        client = self._client

        channels = []
        cursor = None
        types = "public_channel,private_channel" if include_private else "public_channel"

        while True:
            try:
                response = self._client.conversations_list(
                    types=types,
                    limit=min(limit - len(channels), 200),
                    cursor=cursor,
                    exclude_archived=True,
                )
            except SlackApiError as e:
                raise RuntimeError(f"Slack API error: {e.response['error']}")

            for channel in response.get("channels", []):
                channels.append(
                    {
                        "id": channel.get("id", ""),
                        "name": channel.get("name", ""),
                        "purpose": channel.get("purpose", {}).get("value", ""),
                        "topic": channel.get("topic", {}).get("value", ""),
                        "member_count": channel.get("num_members", 0),
                        "is_private": channel.get("is_private", False),
                        "is_member": channel.get("is_member", False),
                    }
                )

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor or len(channels) >= limit:
                break

        return sorted(channels, key=lambda x: x["name"])


    def list_users(self, limit: int = 200) -> list[dict]:
        """List workspace users."""
        client = self._client

        try:
            response = self._client.users_list(limit=limit)
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")

        users = []
        for user in response.get("members", []):
            if user.get("deleted"):
                continue
            users.append(
                {
                    "id": user.get("id", ""),
                    "name": user.get("name", ""),
                    "real_name": user.get("real_name", ""),
                    "email": user.get("profile", {}).get("email", ""),
                    "title": user.get("profile", {}).get("title", ""),
                    "is_bot": user.get("is_bot", False),
                }
            )

        return sorted(users, key=lambda x: x["name"])


    def get_channel_members(self, channel: str) -> list[dict]:
        """Get all members of a Slack channel with their user info.

        Args:
            channel: Channel name (without #) or channel ID

        Returns:
            List of member dicts with id, name, real_name, email
        """
        client = self._client

        # Resolve channel name to ID if needed
        channel_id = channel
        if not channel.startswith("C") and not channel.startswith("G"):
            channels = self.list_bot_channels()
            for ch in channels:
                if ch["name"] == channel or ch["name"] == channel.lstrip("#"):
                    channel_id = ch["id"]
                    break
            else:
                raise RuntimeError(f"Channel '{channel}' not found or bot not a member")

        # Get all member IDs in the channel
        member_ids = []
        cursor = None

        while True:
            try:
                kwargs = {"channel": channel_id, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = self._retry_on_ratelimit(self._client.conversations_members, **kwargs)
            except SlackApiError as e:
                raise RuntimeError(f"Slack API error: {e.response['error']}")

            member_ids.extend(response.get("members", []))

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        # Get user info for each member
        all_users = self.list_users(limit=1000)
        user_lookup = {u["id"]: u for u in all_users}

        members = []
        for member_id in member_ids:
            if member_id in user_lookup:
                user = user_lookup[member_id]
                # Skip bots
                if user.get("is_bot"):
                    continue
                members.append(
                    {
                        "id": user["id"],
                        "name": user["name"],
                        "real_name": user.get("real_name", ""),
                        "email": user.get("email", ""),
                    }
                )

        return members


    def get_channel_member_emails(self, channel: str) -> list[str]:
        """Get email addresses of all non-bot members in a Slack channel.

        Args:
            channel: Channel name (without #) or channel ID

        Returns:
            List of email addresses (excludes members without email)
        """
        members = self.get_channel_members(channel)
        return [m["email"] for m in members if m.get("email")]


    def get_user_email(self, user_id: str) -> str | None:
        """Get a user's email address by their Slack user ID.

        Args:
            user_id: Slack user ID (e.g., 'U123ABC')

        Returns:
            Email address or None if not found
        """
        client = self._client

        try:
            response = self._client.users_info(user=user_id)
            user = response.get("user", {})
            return user.get("profile", {}).get("email")
        except SlackApiError:
            return None


    def _format_requester_attribution(self) -> str:
        """Get requester attribution from environment variables.

        When running inside the agent container, SLACK_REQUESTER_ID and SLACK_REQUESTER_NAME
        are set to identify who requested the work.

        Returns:
            Attribution string like "_(requested by <@U123>)_" or empty string.
        """
        requester_id = os.getenv("SLACK_REQUESTER_ID")
        requester_name = os.getenv("SLACK_REQUESTER_NAME")

        if requester_id:
            return f"\n\n_(requested by <@{requester_id}>)_"
        elif requester_name:
            return f"\n\n_(requested by @{requester_name})_"
        return ""


    def send_message(self, 
        channel: str,
        text: str,
        thread_ts: str | None = None,
        no_attribution: bool = False,
    ) -> dict:
        """Send a message to a channel.

        Args:
            channel: Channel name (with or without #) or channel ID
            text: Message text to send
            thread_ts: Optional thread timestamp to reply in thread
            no_attribution: If True, skip adding requester attribution

        Returns:
            Dict with channel, ts, permalink
        """
        client = self._client

        channel_id = channel.lstrip("#")
        if not channel_id.startswith("C") and not channel_id.startswith("G"):
            channels = self.list_bot_channels()
            for ch in channels:
                if ch["name"] == channel_id:
                    channel_id = ch["id"]
                    break
            else:
                raise RuntimeError(f"Channel '{channel}' not found or bot not a member")

        message_text = text
        if not no_attribution:
            attribution = self._format_requester_attribution()
            if attribution:
                message_text = text + attribution

        try:
            kwargs = {"channel": channel_id, "text": message_text}
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            response = self._client.chat_postMessage(**kwargs)
            return {
                "channel": channel_id,
                "ts": response.get("ts", ""),
                "permalink": f"https://slack.com/archives/{channel_id}/p{response.get('ts', '').replace('.', '')}",
            }
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")


    def upload_file(self, 
        channel: str,
        file_path: str,
        title: str | None = None,
        comment: str | None = None,
        thread_ts: str | None = None,
    ) -> dict:
        """Upload a file to a channel."""
        client = self._client

        channel_id = channel.lstrip("#")
        if not channel_id.startswith("C") and not channel_id.startswith("G"):
            channels = self.list_bot_channels()
            for ch in channels:
                if ch["name"] == channel_id:
                    channel_id = ch["id"]
                    break
            else:
                raise RuntimeError(f"Channel '{channel}' not found or bot not a member")

        try:
            kwargs = {
                "channel": channel_id,
                "file": file_path,
            }
            if title:
                kwargs["title"] = title
            if comment:
                kwargs["initial_comment"] = comment
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            response = self._client.files_upload_v2(**kwargs)
            file_info = response.get("file", {})
            return {
                "id": file_info.get("id", ""),
                "name": file_info.get("name", ""),
                "permalink": file_info.get("permalink", ""),
                "url": file_info.get("url_private", ""),
            }
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")


    def list_usergroups(self) -> list[dict]:
        """List all user groups in the workspace."""
        client = self._client

        try:
            response = self._client.usergroups_list(include_users=True)
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")

        groups = []
        for group in response.get("usergroups", []):
            groups.append(
                {
                    "id": group.get("id", ""),
                    "handle": group.get("handle", ""),
                    "name": group.get("name", ""),
                    "description": group.get("description", ""),
                    "users": group.get("users", []),
                    "user_count": len(group.get("users", [])),
                }
            )

        return sorted(groups, key=lambda x: x["handle"])


    def create_usergroup(self, 
        handle: str, name: str, description: str = "", user_ids: list[str] | None = None
    ) -> dict:
        """Create a new user group."""
        client = self._client

        try:
            response = self._client.usergroups_create(
                name=name,
                handle=handle,
                description=description,
            )
            group = response.get("usergroup", {})
            group_id = group.get("id")

            if user_ids and group_id:
                self._client.usergroups_users_update(usergroup=group_id, users=",".join(user_ids))

            return {
                "id": group_id,
                "handle": group.get("handle", ""),
                "name": group.get("name", ""),
            }
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")


    def update_usergroup_users(self, group_id_or_handle: str, user_ids: list[str]) -> dict:
        """Update users in an existing user group."""
        client = self._client

        group_id = group_id_or_handle
        if not group_id.startswith("S"):
            groups = self.list_usergroups()
            for g in groups:
                if g["handle"] == group_id_or_handle:
                    group_id = g["id"]
                    break
            else:
                raise RuntimeError(f"User group '@{group_id_or_handle}' not found")

        try:
            response = self._client.usergroups_users_update(usergroup=group_id, users=",".join(user_ids))
            group = response.get("usergroup", {})
            return {
                "id": group.get("id", ""),
                "handle": group.get("handle", ""),
                "name": group.get("name", ""),
                "users": response.get("users", user_ids),
            }
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")


    def get_message_files(self, channel_id: str, message_ts: str) -> list[dict]:
        """Get files attached to a specific message."""
        client = self._client

        try:
            response = self._client.conversations_replies(
                channel=channel_id,
                ts=message_ts,
                limit=1,
                inclusive=True,
            )
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error: {e.response['error']}")

        messages = response.get("messages", [])
        if not messages:
            return []

        msg = messages[0]
        files = []
        for f in msg.get("files", []):
            files.append(
                {
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "title": f.get("title", ""),
                    "mimetype": f.get("mimetype", ""),
                    "filetype": f.get("filetype", ""),
                    "url_private": f.get("url_private", ""),
                    "size": f.get("size", 0),
                }
            )

        return files


    def download_file(self, url: str, output_path: str) -> str:
        """Download a Slack file to local path."""
        import urllib.request

        token = os.getenv("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN not set")

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as response:
            with open(output_path, "wb") as f:
                f.write(response.read())

        return output_path


    def dump_channel_with_threads(self, 
        channel_name: str,
        limit: int = 500,
        min_replies: int = 0,
    ) -> dict:
        """Dump full channel history with all thread replies expanded.

        Args:
            channel_name: Channel name (without #)
            limit: Maximum messages to fetch from channel
            min_replies: Only include threads with >= this many replies (0 = all)

        Returns:
            Dict with channel info, messages (with replies inline), and stats
        """
        client = self._client
        user_cache = self._get_user_cache()

        channels = self.list_bot_channels()
        channel_id = None
        for ch in channels:
            if ch["name"] == channel_name.lstrip("#"):
                channel_id = ch["id"]
                break

        if not channel_id:
            raise RuntimeError(f"Channel '{channel_name}' not found or bot not a member")

        all_messages = []
        cursor = None

        while len(all_messages) < limit:
            try:
                response = self._retry_on_ratelimit(
                    self._client.conversations_history,
                    channel=channel_id,
                    limit=min(limit - len(all_messages), 200),
                    cursor=cursor,
                )
            except SlackApiError as e:
                raise RuntimeError(f"Slack API error: {e.response['error']}")

            for msg in response.get("messages", []):
                user_id = msg.get("user", "")
                username = user_cache.get(user_id, user_id)
                text = self._resolve_mentions(msg.get("text", ""), user_cache)
                ts = msg.get("ts", "")
                reply_count = msg.get("reply_count", 0)
                thread_ts = msg.get("thread_ts")

                message_data = {
                    "user": username,
                    "user_id": user_id,
                    "text": text,
                    "timestamp": ts,
                    "permalink": f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}",
                    "reply_count": reply_count,
                    "replies": [],
                }

                if reply_count > 0 and (min_replies == 0 or reply_count >= min_replies):
                    try:
                        thread_response = self._retry_on_ratelimit(
                            self._client.conversations_replies,
                            channel=channel_id,
                            ts=thread_ts or ts,
                            limit=200,
                        )
                        for reply in thread_response.get("messages", [])[1:]:
                            reply_user_id = reply.get("user", "")
                            reply_username = user_cache.get(reply_user_id, reply_user_id)
                            reply_text = self._resolve_mentions(reply.get("text", ""), user_cache)
                            reply_ts = reply.get("ts", "")

                            message_data["replies"].append(
                                {
                                    "user": reply_username,
                                    "user_id": reply_user_id,
                                    "text": reply_text,
                                    "timestamp": reply_ts,
                                    "permalink": f"https://slack.com/archives/{channel_id}/p{reply_ts.replace('.', '')}?thread_ts={ts}",
                                }
                            )
                    except SlackApiError:
                        pass

                all_messages.append(message_data)

            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        threads_with_replies = sum(1 for m in all_messages if m["replies"])
        total_replies = sum(len(m["replies"]) for m in all_messages)

        return {
            "channel": channel_name,
            "channel_id": channel_id,
            "messages": all_messages,
            "stats": {
                "total_messages": len(all_messages),
                "threads_fetched": threads_with_replies,
                "total_replies": total_replies,
            },
        }


    def close(self):
        """Close the underlying HTTP session."""
        pass  # WebClient doesn't need explicit close


def _client() -> SlackClient:
    from shared.plugin_sdk import secret
    return SlackClient(bot_token=secret("SLACK_BOT_TOKEN"))


def get_slack_client() -> SlackClient:
    """Get a cached Slack client instance for CLI compatibility."""
    return _client()


def search_messages(*args, **kwargs):
    return _client().search_messages(*args, **kwargs)


def get_channel_history(*args, **kwargs):
    return _client().get_channel_history(*args, **kwargs)


def get_thread_replies(*args, **kwargs):
    return _client().get_thread_replies(*args, **kwargs)


def list_channels(*args, **kwargs):
    return _client().list_channels(*args, **kwargs)


def list_users(*args, **kwargs):
    return _client().list_users(*args, **kwargs)


def get_channel_members(*args, **kwargs):
    return _client().get_channel_members(*args, **kwargs)


def get_channel_member_emails(*args, **kwargs):
    return _client().get_channel_member_emails(*args, **kwargs)


def get_user_email(*args, **kwargs):
    return _client().get_user_email(*args, **kwargs)


def send_message(*args, **kwargs):
    return _client().send_message(*args, **kwargs)


def upload_file(*args, **kwargs):
    return _client().upload_file(*args, **kwargs)


def list_usergroups(*args, **kwargs):
    return _client().list_usergroups(*args, **kwargs)


def create_usergroup(*args, **kwargs):
    return _client().create_usergroup(*args, **kwargs)


def update_usergroup_users(*args, **kwargs):
    return _client().update_usergroup_users(*args, **kwargs)


def get_message_files(*args, **kwargs):
    return _client().get_message_files(*args, **kwargs)


def download_file(*args, **kwargs):
    return _client().download_file(*args, **kwargs)


def dump_channel_with_threads(*args, **kwargs):
    return _client().dump_channel_with_threads(*args, **kwargs)
