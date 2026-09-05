"""
Step 3: put the two previous pieces together into one real function
that can actually tap the screen - but only if the guardrail allows it.

dump_screen.py  -> tells us what's really on screen right now
guardrail.py    -> tells us whether a given label is safe to tap
tap.py (this file) -> finds the element, checks it, and only then taps it
"""

import json
import os
import re
import sys
import time
from datetime import datetime

from dump_screen import (
    dump_current_screen,
    list_interesting_elements,
    find_editable_fields,
    run_adb,
)
from guardrail import (
    check_tap_allowed,
    check_recovery_tap_allowed,
    check_bill_category_tap_allowed,
    check_session_expired_tap_allowed,
    ACTION_SYNONYMS,
    SESSION_EXPIRED_MARKER_TEXT,
)

BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

FREE_CHOICE_LOG_PATH = os.path.join(os.path.dirname(__file__), "logs", "ai_free_choice.log")


def bounds_to_center(bounds: str) -> tuple[int, int]:
    """Turns '[451,208][629,274]' into the (x, y) point in the middle
    of that box - that's where we'll actually tap."""

    x1, y1, x2, y2 = map(int, BOUNDS_PATTERN.match(bounds).groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _clear_field(existing_text: str, redact: bool = False) -> None:
    """Clears whatever's already in the field that was JUST tapped to
    focus it, before any new text gets typed - `input text` only
    appends, it never replaces, so skipping this on a pre-filled field
    silently produces a corrupted value (a real phone number typed on
    top of a stale pre-filled one, observed live on Sahl's sign-in
    screen - this is why every "type" function in this file goes
    through here now, not just the two that used to).

    `redact` swaps the printed value for asterisks of the same length
    (still using the real length for the backspace count) - use this
    for password fields, same discipline as fill_sign_in_form already
    applied to its own "value not printed" logging."""

    if not existing_text:
        return
    shown = "*" * len(existing_text) if redact else existing_text
    print(f"Clearing existing value: {shown!r}")
    run_adb("shell", "input", "keyevent", "123")  # KEYCODE_MOVE_END
    for _ in range(len(existing_text) + 2):  # a couple extra backspaces for safety
        run_adb("shell", "input", "keyevent", "67")  # KEYCODE_DEL
    time.sleep(0.3)


def safe_tap(label: str, xml_path: str | None = None) -> None:
    """Find `label` on the CURRENT screen and tap it - but only if
    guardrail.py says it's allowed. Refuses loudly otherwise.

    If `xml_path` is given, uses that dump instead of taking a fresh
    one - safe ONLY when the caller just took that dump itself and is
    acting on it immediately, with no AI call or other real delay in
    between (the deterministic step-loop branches in navigate_bill.py).
    Every AI-driven call site still omits this and gets a fresh dump,
    since a model call in between is exactly the kind of delay that
    makes the screen's state worth re-verifying."""

    if xml_path is None:
        xml_path = dump_current_screen()

    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["label"] == label and element["clickable"]:
            matching_element = element
            break

    if matching_element is None:
        print(f"Did not find a tappable element labeled {label!r} on the current screen.")
        return

    allowed, reason = check_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"-> Tapping {label!r} at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))


def safe_tap_session_expired_dialog() -> bool:
    """Taps the bill flow's merged "Session Expired ... Ok" dialog -
    see guardrail.check_session_expired_tap_allowed for why this needs
    its own narrow exception instead of going through safe_tap_recovery's
    exact-match RECOVERY_ALLOWED_LABELS. Finds the element by SUBSTRING
    (the label contains "Session Expired" somewhere in a longer merged
    string) rather than exact match, since there's no element whose
    label is exactly "Ok" to look for here."""

    xml_path = dump_current_screen()

    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["clickable"] and element["label"] and SESSION_EXPIRED_MARKER_TEXT in element["label"]:
            matching_element = element
            break

    if matching_element is None:
        print("Did not find a tappable Session Expired dialog on the current screen.")
        return False

    label = matching_element["label"]
    allowed, reason = check_session_expired_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return False

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"-> Tapping session-expired dialog at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))
    return True


def safe_tap_recovery(label: str) -> None:
    """Same as safe_tap, but checked against guardrail.py's separate,
    narrower RECOVERY_ALLOWED_LABELS instead of the main navigation
    allowlist. Use this ONLY from hardcoded, tightly-scoped recovery
    checks (session expired, re-login) - never expose these labels to
    the AI's free choice in the main step loop."""

    xml_path = dump_current_screen()

    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["label"] == label and element["clickable"]:
            matching_element = element
            break

    if matching_element is None:
        print(f"Did not find a tappable element labeled {label!r} on the current screen.")
        return

    allowed, reason = check_recovery_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"-> Tapping {label!r} at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))


def safe_tap_by_prefix(prefix: str, xml_path: str | None = None) -> bool:
    """Like safe_tap, but matches an element whose label STARTS WITH
    `prefix` instead of matching it exactly - for elements whose label
    changes with app state (e.g. "Network Operator" followed by
    whichever company is currently selected). The ACTUAL full label
    found is still run through both of guardrail.py's normal checks -
    matching by prefix only widens which element we look AT, it
    doesn't trust anything new about what's safe to tap.

    See safe_tap's `xml_path` docstring for when it's safe to pass a
    pre-fetched dump instead of taking a fresh one here."""

    if xml_path is None:
        xml_path = dump_current_screen()

    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["label"] and element["label"].startswith(prefix) and element["clickable"]:
            matching_element = element
            break

    if matching_element is None:
        print(f"Did not find a tappable element starting with {prefix!r} on the current screen.")
        return False

    label = matching_element["label"]
    allowed, reason = check_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return False

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"-> Tapping {label!r} at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))
    return True


def safe_tap_first_present(group_name: str, xml_path: str | None = None) -> bool:
    """Taps whichever label in guardrail.ACTION_SYNONYMS[group_name] is
    currently visible and tappable - for one action that's spelled
    differently across flows (e.g. Internet's Pay Bill form submits via
    "Enquire", Landline's via "Proceed" instead). Every member of the
    group is already independently on guardrail.ALLOWED_LABELS and
    still goes through the normal check_tap_allowed below - this only
    changes which already-approved label we look for, it never
    approves anything new.

    Returns False (and taps nothing) if zero or more than one member of
    the group is visible at once - seeing more than one at the same
    time means the group's members aren't actually mutually exclusive
    in practice, which is a bug in ACTION_SYNONYMS to go fix, not
    something to silently guess through.

    See safe_tap's `xml_path` docstring for when it's safe to pass a
    pre-fetched dump instead of taking a fresh one here."""

    group = ACTION_SYNONYMS.get(group_name)
    if not group:
        print(f"No such action-synonym group: {group_name!r}.")
        return False

    if xml_path is None:
        xml_path = dump_current_screen()
    matching_elements = [
        element
        for element in list_interesting_elements(xml_path)
        if element["clickable"] and element["label"] in group
    ]

    if not matching_elements:
        print(f"None of {sorted(group)} is visible/tappable right now.")
        return False

    if len(matching_elements) > 1:
        seen = sorted({e["label"] for e in matching_elements})
        print(f"More than one of {sorted(group)} is visible at once ({seen}) - "
              f"that's ambiguous, refusing to guess which one to tap.")
        return False

    label = matching_elements[0]["label"]
    allowed, reason = check_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return False

    x, y = bounds_to_center(matching_elements[0]["bounds"])
    print(f"-> Tapping {label!r} at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))
    return True


def safe_tap_bill_category(label: str, xml_path: str | None = None) -> None:
    """Same shape as safe_tap_recovery, but checked against guardrail's
    BILL_CATEGORY_DENY_OVERRIDE instead - the single hand-verified
    exception for "Pay Bill". Use this ONLY from navigate_bill.py's
    hardcoded tap of that exact string.

    See safe_tap's `xml_path` docstring for when it's safe to pass a
    pre-fetched dump instead of taking a fresh one here."""

    if xml_path is None:
        xml_path = dump_current_screen()

    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["label"] == label and element["clickable"]:
            matching_element = element
            break

    if matching_element is None:
        print(f"Did not find a tappable element labeled {label!r} on the current screen.")
        return

    allowed, reason = check_bill_category_tap_allowed(label)
    print(reason)

    if not allowed:
        print("-> NOT tapping.")
        return

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"-> Tapping {label!r} at ({x}, {y})")
    run_adb("shell", "input", "tap", str(x), str(y))


def _log_free_choice_action(
    goal: str, all_labels: list[str], action_type: str, target, value_key,
    reasoning: str, taken: bool, refusal_reason: str = "",
) -> None:
    """Appends one JSON-lines record to logs/ai_free_choice.log for
    every free-choice fallback decision - taken or refused. Records
    value_key, never the actual typed value, as the durable, reviewable
    trail of what the AI decided and why - see ai_free_choice_type's
    docstring for why the raw value is deliberately kept out of it."""

    os.makedirs(os.path.dirname(FREE_CHOICE_LOG_PATH), exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "goal": goal,
        "labels_seen": all_labels,
        "action_type": action_type,
        "target": target,
        "value_key": value_key,
        "reasoning": reasoning,
        "taken": taken,
        "refusal_reason": refusal_reason,
    }
    with open(FREE_CHOICE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ============================================================================
# Free-choice AI fallback tier - DELIBERATELY UNRESTRICTED.
#
# Everything above this line runs every tap through guardrail.py's
# allowlist and/or deny-pattern. The two functions below do NOT - they
# are the free-choice fallback tier's tap/type primitives, used only
# when navigate_bill.py's escalation ladder has already tried the
# deterministic state machine AND the allowlist-constrained AI
# classifier and found nothing. See ai_pick.ask_ai_free_choice's
# docstring and navigate_bill.py's gating (fee_confirmation_reached)
# for the full safety reasoning - this was an explicit, informed
# decision, not an oversight. Never call these directly from a general
# step loop without that gating already in place.
#
# What they still DO check: that the target actually exists on a FRESH
# dump right now (a correctness check against a hallucinated target -
# not a content-safety check), and every action, taken or refused, is
# appended to logs/ai_free_choice.log for later review.
# ============================================================================


def ai_free_choice_tap(
    label: str, goal: str = "", all_labels: list[str] | None = None, reasoning: str = "",
) -> bool:
    """Taps `label` with NO allowlist or deny-pattern check. Still
    independently re-verifies the target is actually present and
    clickable in a fresh dump before acting - never trust a
    hallucinated target that isn't really there."""

    xml_path = dump_current_screen()
    matching_element = None
    for element in list_interesting_elements(xml_path):
        if element["label"] == label and element["clickable"]:
            matching_element = element
            break

    if matching_element is None:
        print(f"[AI FREE-CHOICE] Did not find a tappable element labeled {label!r} "
              f"on a fresh dump - refusing.")
        _log_free_choice_action(goal, all_labels or [], "tap", label, None, reasoning,
                                 taken=False, refusal_reason="target not found on a fresh dump")
        return False

    x, y = bounds_to_center(matching_element["bounds"])
    print(f"[AI FREE-CHOICE] Tapping {label!r} at ({x}, {y}) - reasoning: {reasoning}")
    run_adb("shell", "input", "tap", str(x), str(y))
    _log_free_choice_action(goal, all_labels or [], "tap", label, None, reasoning, taken=True)
    return True


def ai_free_choice_type(
    nearby_label: str, value_key: str, value: str,
    goal: str = "", all_labels: list[str] | None = None, reasoning: str = "",
) -> bool:
    """Types `value` into the editable field whose nearby_label matches
    - no allowlist or deny-pattern check, same as ai_free_choice_tap.
    Re-verifies exactly one matching field exists on a fresh dump
    before acting - refuses to guess between multiple matches, same
    discipline as fill_sign_in_form/clear_and_type_field elsewhere.
    Clears the field first too (see _clear_field) - same reasoning.

    `value_key` is logged, never the raw `value` itself - callers must
    NEVER pass SAHL_PASSWORD's value through here (ai_pick.py's
    known_values contract already forbids including it at all, this is
    the second, independent place that secret must never reach)."""

    xml_path = dump_current_screen()
    fields = find_editable_fields(xml_path)
    matches = [f for f in fields if f["nearby_label"] == nearby_label]

    if len(matches) != 1:
        print(f"[AI FREE-CHOICE] Expected exactly one field near {nearby_label!r} "
              f"on a fresh dump, found {len(matches)} - refusing.")
        _log_free_choice_action(
            goal, all_labels or [], "type", nearby_label, value_key, reasoning,
            taken=False, refusal_reason=f"found {len(matches)} matching fields, expected 1",
        )
        return False

    field = matches[0]
    x, y = bounds_to_center(field["bounds"])
    print(f"[AI FREE-CHOICE] Typing into field near {nearby_label!r} "
          f"(value_key={value_key!r}) - reasoning: {reasoning}")
    run_adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.3)
    _clear_field(field["label"] or "")
    run_adb("shell", "input", "text", value)
    _log_free_choice_action(goal, all_labels or [], "type", nearby_label, value_key, reasoning, taken=True)
    return True


def clear_and_type_field_at_position(text: str, position: str) -> bool:
    """Types into a field chosen by LEFT-TO-RIGHT screen position rather
    than by label - some screens (like Sahl's amount screen) have two
    side-by-side EditText fields with no distinguishing label at all,
    so nearby_label can't tell them apart. `position` is "left" or
    "right". Clears out whatever's already in the field first - needed
    for testing several amount tiers in a row on the same screen:
    `input text` only appends, it doesn't replace, so without this
    "300" then "700" would end up typing "300700"."""

    assert position in ("left", "right")

    xml_path = dump_current_screen()
    fields = find_editable_fields(xml_path)

    if not fields:
        print("No input fields found on this screen.")
        return False

    fields_by_x = sorted(fields, key=lambda f: bounds_to_center(f["bounds"])[0])
    field = fields_by_x[0] if position == "left" else fields_by_x[-1]

    x, y = bounds_to_center(field["bounds"])
    print(f"Tapping the {position} field at ({x}, {y}) to focus it...")
    run_adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.3)
    _clear_field(field["label"] or "")

    print(f"Typing: {text!r}")
    run_adb("shell", "input", "text", text)
    return True


def fill_sign_in_form(phone_number: str, password: str) -> bool:
    """Fills Sahl's sign-in form: types phone_number into the EditText
    that ISN'T flagged password="true" in the accessibility tree, and
    password into the one that IS. Confirmed by hand from a real XML
    dump that these two fields have no other distinguishing feature -
    both show the SAME nearby_label (the "+20" country-code icon) - so
    is_password is the only reliable signal. Refuses to guess if the
    screen doesn't have exactly one of each.

    Clears each field before typing (see _clear_field) - the phone field
    is not reliably empty: observed live with a stale pre-filled number
    already in it, which `input text`'s append-only behavior would
    otherwise have typed the real number on top of, producing an
    invalid concatenated value.

    Does not tap Sign in itself - the caller does that separately
    through safe_tap_recovery, same as every other recovery action."""

    xml_path = dump_current_screen()
    fields = find_editable_fields(xml_path)

    password_fields = [f for f in fields if f.get("is_password")]
    other_fields = [f for f in fields if not f.get("is_password")]

    if len(password_fields) != 1 or len(other_fields) != 1:
        print(f"Expected exactly one password field and one other field on the "
              f"sign-in form, found {len(password_fields)} password field(s) and "
              f"{len(other_fields)} other field(s) - refusing to guess.")
        return False

    phone_field = other_fields[0]
    password_field = password_fields[0]

    x, y = bounds_to_center(phone_field["bounds"])
    print(f"Tapping the phone number field at ({x}, {y})...")
    run_adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.3)
    _clear_field(phone_field["label"] or "")
    print(f"Typing phone number: {phone_number!r}")
    run_adb("shell", "input", "text", phone_number)
    time.sleep(0.3)

    x, y = bounds_to_center(password_field["bounds"])
    print(f"Tapping the password field at ({x}, {y})...")
    run_adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.3)
    _clear_field(password_field["label"] or "", redact=True)
    print("Typing password (value not printed).")
    run_adb("shell", "input", "text", password)
    time.sleep(0.3)

    return True


def dismiss_keyboard() -> None:
    """Hides the on-screen keyboard without navigating back. Needed
    after typing into a field - the keyboard can stay open, resize
    the layout, and cover buttons underneath it (like Confirm),
    silently swallowing taps meant for them."""

    run_adb("shell", "input", "keyevent", "111")  # KEYCODE_ESCAPE


def press_back() -> None:
    """Sends the Android Back button. Pure navigation - it can't submit
    a form or trigger a payment, so unlike safe_tap() this doesn't need
    to go through the guardrail's allowlist at all. Used to back out of
    a flow between operator runs."""

    run_adb("shell", "input", "keyevent", "4")  # KEYCODE_BACK


def swipe_up() -> None:
    """Swipes from the lower-middle to the upper-middle of the screen -
    opens the Android launcher's all-apps drawer from its home screen.
    Same category as press_back(): pure navigation, no labeled element
    to target, can't trigger a payment, so this doesn't go through
    guardrail.py's allowlist either.

    Coordinates assume the same 1080x2400 emulator resolution every
    other hardcoded bound in this project already assumes."""

    run_adb("shell", "input", "swipe", "540", "2100", "540", "500", "300")


def clear_and_type_field(
    text: str, expected_nearby_label: str | None = None, xml_path: str | None = None,
) -> bool:
    """Finds the EditText field to type into and fills it in, clearing
    out whatever's already there first - `input text` only appends, so
    a pre-filled field like Sahl's Internet due-amount box would
    otherwise end up "450.3300" instead of "300".

    - If there's exactly one EditText on screen, that's an easy, safe
      choice - use it.
    - If there's more than one, we refuse to guess UNLESS a caller told
      us which one they mean via `expected_nearby_label` (matched
      against the field's nearby_label context, e.g. "Amount").
    - If we can't confidently identify a field, we print what we saw
      and stop rather than typing into the wrong box.

    See safe_tap's `xml_path` docstring for when it's safe to pass a
    pre-fetched dump instead of taking a fresh one here."""

    if xml_path is None:
        xml_path = dump_current_screen()
    fields = find_editable_fields(xml_path)

    if not fields:
        print("No input field (EditText) found on this screen.")
        return False

    if len(fields) == 1:
        field = fields[0]
    else:
        print(f"Found {len(fields)} input fields on this screen - context near each:")
        for f in fields:
            print(f"  - nearby_label={f['nearby_label']!r} bounds={f['bounds']}")

        if expected_nearby_label is None:
            print("Multiple fields and no hint about which one to use - refusing to guess.")
            return False

        matches = [f for f in fields if f["nearby_label"] == expected_nearby_label]
        if len(matches) != 1:
            print(f"Could not uniquely match nearby_label={expected_nearby_label!r} - refusing to guess.")
            return False
        field = matches[0]

    x, y = bounds_to_center(field["bounds"])
    print(f"Tapping input field (nearby_label={field['nearby_label']!r}) at ({x}, {y}) to focus it...")
    run_adb("shell", "input", "tap", str(x), str(y))
    time.sleep(0.3)
    _clear_field(field["label"] or "")

    print(f"Typing: {text!r}")
    run_adb("shell", "input", "text", text)
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python tap.py "Label To Tap"')
        sys.exit(1)

    safe_tap(sys.argv[1])
