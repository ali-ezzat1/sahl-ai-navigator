"""
Slack relay for the bill flow's OTP/mobile-verification screen - posts
an alert when the flow needs a one-time code, then polls the same
channel for a reply containing one, so an external process (this
project's iPhone Shortcuts automation, relaying an incoming SMS) can
supply it without a human sitting at the keyboard.

Two independent Slack credential shapes are supported:

- SLACK_BOT_TOKEN + SLACK_CHANNEL_ID: a real Slack app's bot token,
  scoped to chat:write (to post the alert) and channels:history (or
  groups:history for a private channel) to read the OTP reply back.
  This is the only shape that can actually CLOSE the loop - a plain
  Incoming Webhook can only post, never read replies.
- SLACK_WEBHOOK_URL alone: an Incoming Webhook, post-only. Alerts still
  go out with this, but poll_for_otp_code has nothing to read from and
  will always time out - see its docstring.

Add whichever you have to .env, same style as SAHL_PASSWORD/GEMINI_API_KEY.
"""

import os
import re
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

SLACK_API_BASE = "https://slack.com/api"

# The iPhone Shortcuts automation is asked to prefix its message (e.g.
# "OTP: 123456") so this can't accidentally match an unrelated 6-digit
# number posted by someone chatting in the same channel - still just a
# plain 6-digit search, not anchored to the prefix itself, so it
# degrades gracefully if the prefix is ever left off.
OTP_CODE_PATTERN = re.compile(r"\b(\d{6})\b")

# How far BEFORE the alert's own timestamp poll_for_otp_code looks for
# a code. The iPhone Shortcut fires the instant the OTP SMS lands,
# which is routinely BEFORE this script has finished detecting the OTP
# screen and posting its alert - confirmed live: the code reached
# Slack at 14:27, the alert went out at 14:28, and the original
# "strictly after the alert" filter never saw it. The phone usually
# wins that race, so the window has to reach backwards.
#
# Bounded rather than unbounded on purpose: an unbounded scan would
# happily return a long-expired code from an earlier run (or a setup
# test message still sitting in the channel), type it in, and fail
# Verify while the log claims it found a code - failing
# silently-correct is the worst outcome here. 120s is roughly the
# OTP's own validity, given the screen offers a resend after ~35s, so
# a code too old for this window is too old to work anyway.
OTP_LOOKBACK_SECONDS = 120.0


class SlackNotConfigured(Exception):
    """Raised when neither a bot token nor a webhook URL is set in .env
    - there's nothing to post the alert to."""


def post_slack_alert(text: str) -> str | None:
    """Posts `text` to Slack. Returns the message's `ts` (Slack's own
    message timestamp) when posted via the bot token - poll_for_otp_code
    uses this as the "only look at replies after this point" boundary -
    or None when only a webhook is configured, since a webhook response
    carries no usable timestamp (poll_for_otp_code has no way to read
    anything back in that case anyway, see its docstring).

    Prefers the bot token when both are set, since only the bot token
    lets poll_for_otp_code read anything back at all."""

    if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        response = requests.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json={"channel": SLACK_CHANNEL_ID, "text": text},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            print(f"Slack chat.postMessage failed: {payload.get('error')}")
            return None
        return payload["ts"]

    if SLACK_WEBHOOK_URL:
        response = requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
        response.raise_for_status()
        return None

    raise SlackNotConfigured(
        "neither SLACK_BOT_TOKEN+SLACK_CHANNEL_ID nor SLACK_WEBHOOK_URL is set in .env"
    )


def poll_for_otp_code(
    after_ts: str | None, timeout_seconds: float = 90.0, poll_interval: float = 5.0,
) -> str | None:
    """Polls the Slack channel's history for a message containing a
    6-digit code, up to timeout_seconds. Returns the newest code found,
    or None on timeout.

    The search window starts OTP_LOOKBACK_SECONDS *before* `after_ts`
    (the alert's own timestamp), not at it - see that constant for why
    the code routinely arrives before the alert does. Two consequences
    that look subtle but matter:

    - The alert itself is now inside the window, and it contains a
      6-digit run of its own unless its timestamp is second-resolution
      (`datetime.now().isoformat()` renders microseconds as exactly six
      digits). It's skipped by `ts` below - exact, and independent of
      who posted what. Note that filtering on sender would NOT work:
      the Shortcut posts through the incoming webhook, so the real code
      is attributed to the same app as the alert, and "ignore bot
      messages" would discard the one message we need.
    - A backwards window can legitimately contain more than one code,
      so the NEWEST match wins rather than the first one Slack happens
      to return.

    Known limitation: if the flow hits the OTP screen twice within
    OTP_LOOKBACK_SECONDS, a poll can still re-find the first
    (already-used) code when the replacement hasn't been posted yet.
    Taking the newest match covers the common case; a full guard
    (remembering codes already typed this run) was deliberately
    deferred as speculative.

    Requires SLACK_BOT_TOKEN + SLACK_CHANNEL_ID - a webhook alone can
    only post, never read a channel's history, so this always returns
    None (after waiting out the full timeout, to give a human watching
    the channel the same window they'd otherwise get) if only
    SLACK_WEBHOOK_URL is set. Get bot-token access before relying on
    this half of the pipeline; posting the alert alone still works
    either way."""

    if not (SLACK_BOT_TOKEN and SLACK_CHANNEL_ID):
        print("SLACK_BOT_TOKEN/SLACK_CHANNEL_ID not set - only a webhook is configured, "
              "which can't read Slack replies. Waiting out the timeout with no way to "
              "find a code.")
        time.sleep(timeout_seconds)
        return None

    oldest = str(float(after_ts) - OTP_LOOKBACK_SECONDS) if after_ts else "0"

    waited = 0.0
    while waited < timeout_seconds:
        response = requests.get(
            f"{SLACK_API_BASE}/conversations.history",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params={"channel": SLACK_CHANNEL_ID, "oldest": oldest, "inclusive": "true"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            print(f"Slack conversations.history failed: {payload.get('error')}")
        else:
            # Pick the newest match explicitly rather than trusting the
            # order Slack returns them in - see the docstring for why
            # the window can hold more than one code, and why our own
            # alert has to be skipped by ts.
            newest_ts, newest_code = None, None
            for message in payload.get("messages", []):
                if after_ts is not None and message.get("ts") == after_ts:
                    continue
                match = OTP_CODE_PATTERN.search(message.get("text", ""))
                if not match:
                    continue
                if newest_ts is None or float(message["ts"]) > float(newest_ts):
                    newest_ts, newest_code = message["ts"], match.group(1)

            if newest_code is not None:
                print(f"Found an OTP code in Slack (message ts={newest_ts}).")
                return newest_code

        time.sleep(poll_interval)
        waited += poll_interval

    print(f"No OTP code appeared in Slack within {timeout_seconds}s.")
    return None


if __name__ == "__main__":
    ts = post_slack_alert(f"slack_otp.py test alert at {datetime.now().isoformat()}")
    print(f"Posted alert, ts={ts!r}. Reply in the channel with a 6-digit code within 30s...")
    code = poll_for_otp_code(ts, timeout_seconds=30.0)
    print(f"Got code: {code!r}")
