import json
import random
import requests
import logging
from CTFd.models import db
from ..models.config import ContainerConfig

logger = logging.getLogger(__name__)


def _get_category_emojis():
    """Parse container_category_emojis JSON; return dict category -> list of emoji strings."""
    raw = ContainerConfig.get('container_category_emojis', '') or DEFAULT_CATEGORY_EMOJIS_JSON
    try:
        return json.loads(raw.strip() or '{}')
    except (json.JSONDecodeError, TypeError):
        return json.loads(DEFAULT_CATEGORY_EMOJIS_JSON)


def _get_emoji_for_category(category):
    """Return one random emoji string for category (lowercase), or empty string."""
    if not category:
        return ""
    emojis_map = _get_category_emojis()
    choices = emojis_map.get((category or "").strip().lower(), [])
    return random.choice(choices) if choices else ""


# Discord/Slack :shortcode: -> Unicode for WhatsApp (WhatsApp does not interpret :name:)
_DISCORD_EMOJI_TO_UNICODE = {
    ":knife:": "\U0001f52a",  # 🔪
    ":drop_of_blood:": "\U0001fa78",  # 🩸
    ":globe_with_meridians:": "\U0001f310",  # 🌐
    ":sob:": "\U0001f62d",  # 😭
    ":closed_lock_with_key:": "\U0001f510",  # 🔐
    ":bug:": "\U0001f41b",  # 🐛
    ":rewind:": "\u23ea",  # ⏪
    ":mag:": "\U0001f50d",  # 🔍
    ":detective:": "\U0001f575",  # 🕵
    ":white_large_square:": "\u2b1c",  # ⬜
    ":chains:": "\u26d3",  # ⛓
    ":jigsaw:": "\U0001f9e9",  # 🧩
}


def _discord_emoji_to_unicode(text):
    """Replace Discord :shortcode: with Unicode emoji for WhatsApp."""
    if not text:
        return text
    out = text
    for shortcode, unicode_char in _DISCORD_EMOJI_TO_UNICODE.items():
        out = out.replace(shortcode, unicode_char)
    return out


def _discord_to_whatsapp_markdown(text):
    """
    Convert Discord-style markdown to WhatsApp-style so bold/underline render correctly.
    Discord: **bold**, __underline__. WhatsApp: *bold*, _italic_.
    Also replaces :emoji: shortcodes with Unicode for WhatsApp.
    """
    if not text:
        return text
    out = (
        text.replace("**__", "*")
        .replace("__**", "*")
        .replace("**", "*")
        .replace("__", "_")
    )
    return _discord_emoji_to_unicode(out)


# Default first-blood Discord message template (placeholders: chal_name, user_name, team_name, emojis)
DEFAULT_FIRST_BLOOD_MESSAGE = (
    ":knife::drop_of_blood: First Blood for challenge **{chal_name}** "
    "goes to **{user_name}** of team **__{team_name}__**! {emojis}"
)
# Default solve (non-first-blood) message template
DEFAULT_SOLVE_MESSAGE = "**{user_name}** of **__{team_name}__** just solved **{chal_name}**! {emojis}"
# Default category emojis JSON (reference: config.CATEGORY_EMOJIS)
DEFAULT_CATEGORY_EMOJIS_JSON = (
    '{"web": [":globe_with_meridians:"], "crypto": [":sob::closed_lock_with_key:"], '
    '"pwn": [":bug:"], "rev": [":rewind:"], "forensics": [":mag:"], "osint": [":detective:"], '
    '"blockchain": [":white_large_square::chains:"], "misc": [":jigsaw:"]}'
)


class NotificationService:
    def __init__(self):
        self.webhook_url = None

    def _get_webhook_url(self):
        return ContainerConfig.get('container_discord_webhook_url', '')

    # -------------------------------------------------------------------------
    # WaSender helpers
    # -------------------------------------------------------------------------

    def _get_wa_config(self):
        """Return (api_key, group_id, image_url, audio_url) from ContainerConfig."""
        return (
            ContainerConfig.get('wasender_api_key', ''),
            ContainerConfig.get('wasender_group_id', ''),
            ContainerConfig.get('wasender_image_url', ''),
            ContainerConfig.get('wasender_audio_url', ''),
        )

    def _build_wa_text(self, title, message, fields=None):
        """Convert Discord embed-style data to plain WhatsApp text."""
        lines = [f"*{title}*", message]
        if fields:
            lines.append("")
            for f in fields:
                lines.append(f"*{f['name']}:* {f['value']}")
        return "\n".join(lines)

    def _send_whatsapp(self, text, api_key=None, group_id=None,
                       image_url=None, audio_url=None):
        """
        Send text (+ optional image/audio) to a WhatsApp group via WaSender.

        If api_key/group_id are not provided, they are read from ContainerConfig.
        Returns True if at least the text message was sent successfully.
        """
        _api_key, _group_id, _img_url, _aud_url = self._get_wa_config()

        api_key   = api_key   or _api_key
        group_id  = group_id  or _group_id
        image_url = image_url if image_url is not None else _img_url
        audio_url = audio_url if audio_url is not None else _aud_url

        if not api_key or not group_id:
            return False

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        base_url = "https://api.wasenderapi.com/api/send-message"

        try:
            # Message payload — include imageUrl if we have one
            payload = {"to": group_id, "text": text}
            if image_url:
                payload["imageUrl"] = image_url

            resp = requests.post(base_url, json=payload, headers=headers, timeout=10)
            success = resp.status_code == 200

            # Send audio as a separate POST (WaSender does not support caption+audio)
            if audio_url:
                audio_payload = {"to": group_id, "audioUrl": audio_url}
                requests.post(base_url, json=audio_payload, headers=headers, timeout=10)

            return success
        except Exception as e:
            logger.error(f"Failed to send WaSender notification: {e}")
            return False

    def upload_media(self, file_bytes, mime_type, api_key=None):
        """
        Upload raw bytes to WaSender CDN using raw-binary POST.

        Sends file_bytes directly with Content-Type = mime_type.
        Returns the publicUrl string on success, raises RuntimeError on failure.
        """
        _api_key, _, _, _ = self._get_wa_config()
        api_key = api_key or _api_key

        if not api_key:
            raise RuntimeError("WaSender API key is not configured")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": mime_type,
        }
        resp = requests.post(
            "https://api.wasenderapi.com/api/upload",
            data=file_bytes,
            headers=headers,
            timeout=30,
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"WaSender upload failed ({resp.status_code}): {resp.text}"
            )

        body = resp.json()
        if not body.get("success"):
            raise RuntimeError(f"WaSender upload error: {body}")

        return body["publicUrl"]

    def send_wa_test(self, api_key=None, group_id=None):
        """Send a plain-text test message to the WhatsApp group."""
        return self._send_whatsapp(
            "✅ *WaSender Connection Test*\nYour WhatsApp integration is configured correctly!",
            api_key=api_key,
            group_id=group_id,
            image_url="",
            audio_url="",
        )

    # -------------------------------------------------------------------------
    # Discord helpers
    # -------------------------------------------------------------------------

    def send_alert(self, title, message, color=0xff0000, fields=None):
        """
        Send an alert to Discord and WaSender.

        Args:
            title: Embed title
            message: Embed description
            color: Hex color integer (default red)
            fields: List of dicts {'name': str, 'value': str, 'inline': bool}
        """
        webhook_url = self._get_webhook_url()
        discord_ok = False
        if webhook_url:
            try:
                payload = {
                    "embeds": [{
                        "title": title,
                        "description": message,
                        "color": color,
                        "fields": fields or []
                    }]
                }
                response = requests.post(webhook_url, json=payload, timeout=5)
                discord_ok = response.status_code == 204
            except Exception as e:
                logger.error(f"Failed to send Discord notification: {e}")

        # Fire WaSender (fire-and-forget, don't let it block/fail the caller)
        try:
            wa_text = self._build_wa_text(title, message, fields)
            self._send_whatsapp(wa_text)
        except Exception as e:
            logger.error(f"WaSender alert failed: {e}")

        return discord_ok

    def notify_cheat(self, user, challenge, flag, owner):
        """Send cheat detection alert"""
        fields = [
            {"name": "User", "value": user.name if user else "Unknown", "inline": True},
            {"name": "Challenge", "value": challenge.name if challenge else "Unknown", "inline": True},
            {"name": "Flag Submitted", "value": f"`{flag}`", "inline": False},
            {"name": "Original Owner", "value": owner.name if owner else "Unknown", "inline": True},
            {"name": "Action Taken", "value": "User & Owner Banned", "inline": False}
        ]
        
        return self.send_alert(
            title="🚨 Cheating Detected!",
            message="A user submitted a flag belonging to another team/user.",
            color=0xff0000, # Red
            fields=fields
        )

    def notify_error(self, operation, error_msg):
        """Send system error alert"""
        fields = [
            {"name": "Operation", "value": operation, "inline": True},
            {"name": "Error", "value": f"```{error_msg}```", "inline": False}
        ]
        
        return self.send_alert(
            title="⚠️ Container Plugin Error",
            message="An error occurred in the container system.",
            color=0xffa500, # Orange
            fields=fields
        )

    def _post_announcer_and_leaderboard(self, first_blood, chal_name, user_name, team_name, chal_id, category, points):
        """If container_announcer_url set: POST /api/blood or /api/solves, then POST /api/leaderboard with top 10."""
        announcer_url = (ContainerConfig.get('container_announcer_url', '') or '').strip().rstrip('/')
        if not announcer_url:
            return
        try:
            ep = 'blood' if first_blood else 'solves'
            requests.post(
                f"{announcer_url}/api/{ep}",
                json={
                    "points": points or 0,
                    "category": category or "",
                    "chal_name": chal_name,
                    "team_name": team_name,
                    "solved_by": user_name,
                    "first_blood": first_blood,
                },
                timeout=5,
            )
            from CTFd.utils.scores import get_standings
            standings = get_standings(count=10)
            teams = [
                {"name": row.name, "points": int(row.score), "position": pos}
                for pos, row in enumerate(standings, 1)
            ]
            requests.post(f"{announcer_url}/api/leaderboard", json=teams, timeout=5)
        except Exception as e:
            logger.warning("Announcer URL request failed: %s", e)

    def notify_first_blood(self, user, team, challenge, emojis=None):
        """
        Send first-blood announcement to Discord (content-only message).
        Uses container_first_blood_webhook_url if set, else container_discord_webhook_url.
        Message template supports {chal_name}, {user_name}, {team_name}, {emojis}.
        Optionally POSTs to container_announcer_url /api/blood and /api/leaderboard.
        """
        enabled = (ContainerConfig.get('container_first_blood_enabled', '') or '').strip().lower() == 'true'
        if not enabled:
            logger.info("First blood notification skipped (disabled or not configured).")
            return False

        webhook_url = (
            ContainerConfig.get('container_first_blood_webhook_url', '').strip()
            or self._get_webhook_url()
        )
        if not webhook_url:
            logger.info("First blood notification skipped (no Discord webhook URL).")
            return False

        template = (
            ContainerConfig.get('container_first_blood_message', '').strip()
            or DEFAULT_FIRST_BLOOD_MESSAGE
        )
        user_name = user.name if user else "Unknown"
        team_name = team.name if team else (user.name if user else "Solo")
        chal_name = challenge.name if challenge else "Unknown"
        if emojis is None and challenge:
            emojis = _get_emoji_for_category(getattr(challenge, 'category', None))
        emojis = emojis or ""
        points = getattr(challenge, 'value', 0) if challenge else 0
        category = getattr(challenge, 'category', None) if challenge else None
        chal_id = getattr(challenge, 'id', 0) if challenge else 0
        try:
            message = template.format(
                chal_name=chal_name,
                user_name=user_name,
                team_name=team_name,
                emojis=emojis,
            )
        except KeyError as e:
            logger.warning("First-blood template has invalid placeholder: %s", e)
            message = DEFAULT_FIRST_BLOOD_MESSAGE.format(
                chal_name=chal_name,
                user_name=user_name,
                team_name=team_name,
                emojis=emojis,
            )

        discord_ok = False
        try:
            response = requests.post(
                webhook_url,
                json={"content": message},
                timeout=5,
            )
            discord_ok = response.status_code in (200, 204)
            if discord_ok:
                logger.info("First blood announced for challenge %s (Discord).", chal_name)
            else:
                logger.warning("First blood Discord returned status %s for challenge %s.", response.status_code, chal_name)
        except Exception as e:
            logger.error("Failed to send first-blood Discord notification: %s", e)

        # Also send to WhatsApp using the configured WaSender group ID (convert Discord markdown to WhatsApp)
        try:
            wa_text = _discord_to_whatsapp_markdown(message)
            self._send_whatsapp(wa_text, image_url="", audio_url="")
        except Exception as e:
            logger.error("Failed to send first-blood WhatsApp notification: %s", e)

        self._post_announcer_and_leaderboard(
            first_blood=True,
            chal_name=chal_name,
            user_name=user_name,
            team_name=team_name,
            chal_id=chal_id,
            category=category,
            points=points,
        )
        return discord_ok

    def announce_solve(self, user, team, challenge):
        """
        Announce a non-first-blood solve (reference: SOLVE_WEBHOOK_URL, SOLVE_ANNOUNCE_STRING).
        Uses container_solve_webhook_url or main Discord webhook. Optionally announcer URL + leaderboard.
        """
        webhook_url = (
            ContainerConfig.get('container_solve_webhook_url', '').strip()
            or self._get_webhook_url()
        )
        if not webhook_url:
            return False
        template = (
            ContainerConfig.get('container_solve_message', '').strip()
            or DEFAULT_SOLVE_MESSAGE
        )
        user_name = user.name if user else "Unknown"
        team_name = team.name if team else (user.name if user else "Solo")
        chal_name = challenge.name if challenge else "Unknown"
        emojis = _get_emoji_for_category(getattr(challenge, 'category', None) if challenge else None) or ""
        points = getattr(challenge, 'value', 0) if challenge else 0
        category = getattr(challenge, 'category', None) if challenge else None
        chal_id = getattr(challenge, 'id', 0) if challenge else 0
        try:
            message = template.format(
                chal_name=chal_name,
                user_name=user_name,
                team_name=team_name,
                emojis=emojis,
            )
        except KeyError:
            message = DEFAULT_SOLVE_MESSAGE.format(
                chal_name=chal_name,
                user_name=user_name,
                team_name=team_name,
                emojis=emojis,
            )
        try:
            response = requests.post(webhook_url, json={"content": message}, timeout=5)
            ok = response.status_code in (200, 204)
        except Exception as e:
            logger.error("Failed to send solve Discord notification: %s", e)
            ok = False
        try:
            wa_text = _discord_to_whatsapp_markdown(message)
            self._send_whatsapp(wa_text, image_url="", audio_url="")
        except Exception as e:
            logger.error("Failed to send solve WhatsApp notification: %s", e)
        self._post_announcer_and_leaderboard(
            first_blood=False,
            chal_name=chal_name,
            user_name=user_name,
            team_name=team_name,
            chal_id=chal_id,
            category=category,
            points=points,
        )
        return ok

    def send_demo_first_blood(self):
        """
        Send a demo first-blood message using the current template and webhook (for testing).
        Uses the same path as real first blood; enable and webhook must be set.
        """
        class _Mock:
            pass
        user, team, challenge = _Mock(), _Mock(), _Mock()
        user.name = "TestUser"
        team.name = "TestTeam"
        challenge.name = "Demo Challenge"
        return self.notify_first_blood(user, team, challenge)

    def send_test(self, webhook_url=None):
        """Send a simple test message"""
        url_to_use = webhook_url or self._get_webhook_url()
        return self._send_raw(
            url_to_use,
            title="✅ Connection Test",
            message="Your Discord Webhook is configured correctly!",
            color=0x00ff00 # Green
        )

    def send_demo_cheat(self, webhook_url=None):
        """Send a demo cheat alert (Discord only)"""
        url_to_use = webhook_url or self._get_webhook_url()
        fields = [
            {"name": "User", "value": "demo_hacker", "inline": True},
            {"name": "Challenge", "value": "Demo Challenge", "inline": True},
            {"name": "Flag Submitted", "value": "`CTF{demo_flag_hash}`", "inline": False},
            {"name": "Original Owner", "value": "innocent_victim", "inline": True},
            {"name": "Action Taken", "value": "User & Owner Banned", "inline": False}
        ]
        return self._send_raw(
            url_to_use,
            title="🚨 Cheating Detected! (DEMO)",
            message="This is a DEMO alert. No actual banning occurred.",
            color=0xff0000, # Red
            fields=fields
        )

    def send_demo_error(self, webhook_url=None):
        """Send a demo error alert (Discord only)"""
        url_to_use = webhook_url or self._get_webhook_url()
        fields = [
            {"name": "Operation", "value": "Container Provisioning", "inline": True},
            {"name": "Error", "value": "```DockerException: Connection refused```", "inline": False}
        ]
        return self._send_raw(
            url_to_use,
            title="⚠️ Plugin Error (DEMO)",
            message="This is a DEMO alert.",
            color=0xffa500, # Orange
            fields=fields
        )

    def send_wa_test_image(self, api_key=None, group_id=None):
        """Send a test message that includes the stored image URL."""
        _, _, image_url, _ = self._get_wa_config()
        if not image_url:
            return False  # no image stored yet
        return self._send_whatsapp(
            "🖼️ *Image Test*\nThis is a test message with the stored alert image.",
            api_key=api_key, group_id=group_id,
            image_url=image_url, audio_url="",
        )

    def send_wa_test_audio(self, api_key=None, group_id=None):
        """Send a test message that plays the stored audio URL."""
        _, _, _, audio_url = self._get_wa_config()
        if not audio_url:
            return False  # no audio stored yet
        return self._send_whatsapp(
            "🔊 *Audio Test*\nThis is a test message with the stored alert audio.",
            api_key=api_key, group_id=group_id,
            image_url="", audio_url=audio_url,
        )

    def send_wa_demo_cheat(self, api_key=None, group_id=None):
        """Send a demo cheat alert to WhatsApp."""
        fields = [
            {"name": "User", "value": "demo_hacker"},
            {"name": "Challenge", "value": "Demo Challenge"},
            {"name": "Flag Submitted", "value": "CTF{demo_flag_hash}"},
            {"name": "Original Owner", "value": "innocent_victim"},
            {"name": "Action Taken", "value": "User & Owner Banned"},
        ]
        text = self._build_wa_text(
            "🚨 Cheating Detected! (DEMO)",
            "This is a DEMO alert. No actual banning occurred.",
            fields,
        )
        return self._send_whatsapp(text, api_key=api_key, group_id=group_id,
                                   image_url="", audio_url="")

    def send_wa_demo_error(self, api_key=None, group_id=None):
        """Send a demo error alert to WhatsApp."""
        fields = [
            {"name": "Operation", "value": "Container Provisioning"},
            {"name": "Error", "value": "DockerException: Connection refused"},
        ]
        text = self._build_wa_text(
            "⚠️ Plugin Error (DEMO)",
            "This is a DEMO alert.",
            fields,
        )
        return self._send_whatsapp(text, api_key=api_key, group_id=group_id,
                                   image_url="", audio_url="")

    def _send_raw(self, url, title, message, color, fields=None):
        """Internal method to send to a specific Discord URL"""
        if not url:
            return False
        
        try:
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": color,
                    "fields": fields or []
                }]
            }
            response = requests.post(url, json=payload, timeout=5)
            return response.status_code == 204
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")
            return False
