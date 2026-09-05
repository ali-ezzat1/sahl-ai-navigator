"""
Step 4: ask an AI which of the currently-safe labels to tap next,
instead of us deciding by hand every time.

Important: this function can ONLY return one of the labels it was
given - it's not allowed to invent a new one. That's what keeps this
compatible with guardrail.py: the AI narrows down which already-safe
option to pick, it never gets to introduce something new and unvetted.
"""

import json
import os
from dotenv import load_dotenv

from google import genai
from google.genai import types

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.5-flash-lite"  # cheap, fast - fine for picking from a short list

client = genai.Client(api_key=API_KEY) if API_KEY else None


def ask_ai_which_label(goal: str, available_labels: list[str]) -> str | None:
    """Given a goal in plain English and the list of labels that are
    currently BOTH visible on screen AND already approved as safe,
    ask the AI which one to tap next. Returns None if it fails to
    pick something from the list we actually gave it."""

    if client is None:
        raise RuntimeError("GEMINI_API_KEY is not set - can't call the AI.")

    options_list = "\n".join(f"- {label}" for label in available_labels)

    prompt = f"""You are choosing which button to tap next in a mobile app,
to make progress toward this goal:

GOAL: {goal}

You may ONLY choose one of these exact options (copy it exactly,
nothing else, no explanation, no punctuation):

{options_list}

Reply with just the one option that makes the most progress toward the goal."""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=30),
    )

    choice = response.text.strip()

    if choice not in available_labels:
        print(f"AI returned {choice!r}, which wasn't one of the options given - refusing.")
        return None

    return choice


SCREEN_TYPES = [
    "home",
    "phone-and-operator-form",
    "amount-entry",
    "fee-confirmation",
    "unknown",
]


def classify_screen_and_pick_action(
    goal: str,
    all_labels: list[str],
    candidates: list[str],
    screen_types: list[str] | None = None,
) -> dict:
    """One AI call that does two things at once:

    1. Classifies what kind of screen this is, reasoning over
       EVERYTHING currently visible (all_labels) - structural
       reasoning instead of hardcoded exact-string markers, so it
       survives label renames and minor layout changes.
    2. Picks which ONE label to tap next toward the goal - but ONLY
       from `candidates`, which the caller has already filtered down
       to guardrail.py's ALLOWED_LABELS. The AI sees the full screen
       for CONTEXT only - it can never choose, and we never trust it
       to have chosen, anything outside that pre-approved list.

    `screen_types`: the closed set of category names the AI is allowed
    to classify this screen as. Defaults to SCREEN_TYPES (Mobile
    Top-Up's taxonomy) so existing callers that don't pass this keep
    working unchanged - a different flow (e.g. the bill-pay flow) can
    pass its own list here instead of getting a second copy of this
    function.

    Returns {"screen_type": str, "action": str | None}. `action` is
    None if nothing in `candidates` makes sense right now, or if the
    AI's response can't be trusted (bad JSON, or it tried to pick
    something outside the approved list - we verify this ourselves,
    we don't just take the AI's word for it)."""

    if screen_types is None:
        screen_types = SCREEN_TYPES

    if client is None:
        raise RuntimeError("GEMINI_API_KEY is not set - can't call the AI.")

    labels_block = "\n".join(f"- {label}" for label in all_labels) or "(nothing labeled here)"
    candidates_block = "\n".join(f"- {label}" for label in candidates) or "(none approved right now)"
    screen_types_block = ", ".join(screen_types)

    prompt = f"""You are helping navigate a mobile app screen by screen toward this goal:

GOAL: {goal}

Everything currently visible on this screen (for context only, to help
you understand what screen this is):
{labels_block}

Classify this screen as exactly one of: {screen_types_block}

Separately, choose which ONE action to take next. You may ONLY choose
from this pre-approved list - nothing else is allowed, even if
something else on screen looks more relevant:
{candidates_block}

Reply with STRICT JSON only, no other text, in exactly this shape:
{{"screen_type": "<one category from the list above>", "action": "<one exact label from the approved list above, or null if none of them make sense right now>"}}"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=200,
            response_mime_type="application/json",
        ),
    )

    try:
        result = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        print(f"AI did not return valid JSON - refusing. Raw response: {response.text!r}")
        return {"screen_type": "unknown", "action": None}

    screen_type = result.get("screen_type")
    if screen_type not in screen_types:
        print(f"AI returned an unrecognized screen_type {screen_type!r} - treating as 'unknown'.")
        screen_type = "unknown"

    action = result.get("action")
    if action is not None and action not in candidates:
        print(f"AI returned action {action!r}, which wasn't one of the approved options - refusing it.")
        action = None

    return {"screen_type": screen_type, "action": action}


def ask_ai_free_choice(goal: str, all_labels: list[str], elements: list[dict], known_values: dict) -> dict:
    """The free-choice fallback tier: unlike ask_ai_which_label and
    classify_screen_and_pick_action, this is NOT restricted to a
    pre-approved candidate list - the AI can choose to tap ANY visible,
    currently-present element, or type one of the caller-supplied
    known_values into any visible editable field, based on the goal.
    It does NOT check guardrail.ALLOWED_LABELS or guardrail.DENY_PATTERN.

    This is deliberately the least restricted function in this project
    - an explicit, informed exception, not a default. See
    navigate_bill.py's escalation ladder and fee_confirmation_reached
    gating for the safety reasoning around when it's allowed to run at
    all, and its module docstring for why a completed payment isn't
    reachable through a tap alone even here.

    known_values is a plain dict of task-relevant values the AI may
    choose to type (e.g. {"operator": "WE"}) - it can only reference
    these by KEY, never invent new text to type. SAHL_PASSWORD must
    NEVER be included in known_values by any caller - every key AND
    VALUE in it goes directly into the prompt sent to the AI.

    Returns {"action_type": "tap"|"type"|"none", "target_label": str|None,
    "target_nearby_label": str|None, "value_key": str|None,
    "reasoning": str}. Every returned target/key is re-verified by THIS
    function against what's actually tappable/editable and what's
    actually in known_values before being returned - same "verify what
    the AI said, don't just trust it" pattern as everywhere else in
    this file. This function only asks, it never taps or types
    anything itself - see tap.ai_free_choice_tap/ai_free_choice_type."""

    if client is None:
        raise RuntimeError("GEMINI_API_KEY is not set - can't call the AI.")

    tappable = [e for e in elements if e["clickable"] and e["label"]]
    typeable = [e for e in elements if e["class"] == "android.widget.EditText"]

    tappable_block = "\n".join(f"- {e['label']}" for e in tappable) or "(nothing tappable here)"
    typeable_block = "\n".join(
        f"- field near {e['nearby_label']!r} (currently: {e['label']!r})" for e in typeable
    ) or "(no editable fields here)"
    labels_block = "\n".join(f"- {label}" for label in all_labels) or "(nothing labeled here)"
    known_values_block = "\n".join(f"- {k}: {v}" for k, v in known_values.items()) or "(none supplied)"

    prompt = f"""You are navigating a mobile app screen that didn't match
any known pattern, trying to make progress toward this goal:

GOAL: {goal}

Everything currently visible on this screen:
{labels_block}

Elements you could TAP:
{tappable_block}

Editable text fields you could TYPE into:
{typeable_block}

Values you're allowed to type - ONLY these, never invent new text:
{known_values_block}

Decide ONE of:
- Tap one of the tappable elements above (copy its label EXACTLY).
- Type one of the known values above into one of the editable fields
  above (identify the field by its "field near ..." description
  exactly, and the value by its key name exactly).
- Do nothing, if none of the above make sense right now.

Reply with STRICT JSON only, no other text, in exactly this shape:
{{"action_type": "tap" or "type" or "none", "target_label": "<exact tappable label, only if action_type is tap, else null>", "target_nearby_label": "<exact field-near description, only if action_type is type, else null>", "value_key": "<exact key from the known values above, only if action_type is type, else null>", "reasoning": "<one short sentence>"}}"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=200,
            response_mime_type="application/json",
        ),
    )

    try:
        result = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        print(f"AI did not return valid JSON for the free-choice fallback - refusing. "
              f"Raw response: {response.text!r}")
        return {"action_type": "none", "target_label": None, "target_nearby_label": None,
                "value_key": None, "reasoning": "invalid JSON response"}

    action_type = result.get("action_type")
    if action_type not in ("tap", "type", "none"):
        action_type = "none"

    target_label = result.get("target_label")
    target_nearby_label = result.get("target_nearby_label")
    value_key = result.get("value_key")
    reasoning = result.get("reasoning", "")

    if action_type == "tap" and target_label not in {e["label"] for e in tappable}:
        print(f"Free-choice tap target {target_label!r} isn't actually tappable "
              f"right now - refusing.")
        action_type, target_label = "none", None

    if action_type == "type":
        if value_key not in known_values:
            print(f"Free-choice value_key {value_key!r} isn't one of the known "
                  f"values - refusing.")
            action_type = "none"
        elif target_nearby_label not in {e["nearby_label"] for e in typeable}:
            print(f"Free-choice type target {target_nearby_label!r} doesn't match "
                  f"any editable field on screen - refusing.")
            action_type = "none"

    return {
        "action_type": action_type,
        "target_label": target_label if action_type == "tap" else None,
        "target_nearby_label": target_nearby_label if action_type == "type" else None,
        "value_key": value_key if action_type == "type" else None,
        "reasoning": reasoning,
    }


def suggest_relabel(expected_labels: list[str], all_labels: list[str]) -> dict | None:
    """When one or more anchor labels we expected to find on this
    screen aren't there, ask the AI whether anything ACTUALLY visible
    looks like the same element under a new name (e.g. "Internet"
    became "DSL"). This is purely a suggestion for a HUMAN to review -
    it never gets tapped, and it never expands what's tappable.
    Approving it still means manually adding the new label to
    guardrail.ALLOWED_LABELS after confirming it yourself. Returns
    {"expected": ..., "suggested": ..., "reasoning": ...} or None if
    the AI found nothing it was confident about."""

    if client is None or not expected_labels:
        return None

    expected_block = "\n".join(f"- {label}" for label in expected_labels)
    visible_block = "\n".join(f"- {label}" for label in all_labels) or "(nothing labeled here)"

    prompt = f"""We expected to find ONE of these UI element labels on
this screen, but none of them are there:
{expected_block}

Here's everything that IS actually visible right now:
{visible_block}

Is there ONE visible label that's plausibly the SAME element as one of
the expected ones, just renamed or reworded (e.g. "Internet" could
become "DSL", "Broadband", "Wifi Bill")? Only answer if you're
reasonably confident - a wrong guess just wastes a human's time
double-checking it, so prefer null over a weak guess.

Reply with STRICT JSON only, no other text, in exactly this shape:
{{"expected": "<which expected label above you think this replaces, or null>", "suggested": "<the one exact visible label you think matches, or null>", "reasoning": "<one short sentence>"}}"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=150,
            response_mime_type="application/json",
        ),
    )

    try:
        result = json.loads(response.text)
    except (json.JSONDecodeError, TypeError):
        print(f"AI did not return valid JSON when suggesting a relabel - ignoring. Raw: {response.text!r}")
        return None

    suggested = result.get("suggested")
    expected = result.get("expected")
    reasoning = result.get("reasoning", "")

    if not suggested or not expected:
        return None
    if suggested not in all_labels:
        print(f"AI suggested {suggested!r}, which isn't even in the visible labels - ignoring.")
        return None
    if expected not in expected_labels:
        print(f"AI's 'expected' answer {expected!r} wasn't one we asked about - ignoring.")
        return None

    print(f"\nAI's rename guess: {expected!r} -> {suggested!r} ({reasoning})")
    print(f"This is a SUGGESTION only - it was not tapped. If it looks right, confirm")
    print(f"{suggested!r} yourself on the emulator, then add it to guardrail.ALLOWED_LABELS")
    print("by hand before re-running.")
    return {"expected": expected, "suggested": suggested, "reasoning": reasoning}


if __name__ == "__main__":
    # synthetic test, same idea as guardrail.py's test - not reading the
    # real screen yet, just proving the AI call and the "must pick from
    # the list" constraint both work correctly.
    goal = "Get to the Mobile Top-Up flow to see the recharge fee"
    options = ["Mobile", "Electricity", "Water", "Gas", "Landline", "Donations"]

    choice = ask_ai_which_label(goal, options)
    print(f"AI chose: {choice!r}")
