"""
Step 5: put dump_screen.py, guardrail.py, ai_pick.py and tap.py
together into one real loop.

Each pass: look at the real screen -> narrow down to labels that are
BOTH visible right now AND already approved as safe -> ask the AI
to pick one -> tap it (still re-checked by guardrail.py inside
safe_tap) -> repeat, until "Service Fees" shows up on screen.

Some screens are combined FORMS (phone number, then operator, then a
service list that only appears once an operator is picked, all on one
screen) rather than a sequence of separate screens. This app doesn't
expose "already chosen" state anywhere in the accessibility tree - no
operator or service ever shows selected="true" - so we can't just ask
"is this done yet?" directly. Instead we infer progress from what's
now VISIBLE: if the phone field's own text already matches the number
we want, it's filled - don't retype it. If a "Select Service" section
has appeared, that only happens after an operator was chosen - don't
re-tap an operator. This way each pass does exactly the next
incomplete step and nothing already done gets repeated.

The phone number itself is NOT something the AI chooses - it's a
fixed value passed in on purpose, same as the goal.
"""

import re
import time

from dump_screen import dump_current_screen, list_interesting_elements, AdbConnectionError
from guardrail import ALLOWED_LABELS
from launch_sahl import ensure_sahl_open
from ai_pick import ask_ai_which_label, classify_screen_and_pick_action
from tap import safe_tap, safe_tap_recovery, clear_and_type_field, dismiss_keyboard

SESSION_EXPIRED_MARKER = "Session Expired"  # the app logged us out mid-flow
SIGN_IN_SCREEN_MARKER = "Forgot password?"  # distinctive - only appears on the sign-in screen

FEE_SCREEN_MARKER = "Service Fees"  # once this label appears, we've arrived - stop
FORM_SCREEN_MARKER = "Select Operator"  # marks the combined phone/operator/service form
SERVICE_SECTION_MARKER = "Select Service"  # only appears once an operator has been picked
OPERATOR_LABELS = {"Vodafone", "e&", "Orange", "WE"}
MAX_RETRIES_BEFORE_DEAD_END = 1
MAX_FORM_ATTEMPTS = 2  # if the form screen still hasn't budged after this many
                       # tries, something's blocking Confirm - stop, don't loop forever


# The "index + 1" heuristic below assumes the key and its value sit
# right next to each other in the flattened label list - if the UI
# ever inserts something else between them (an icon, a tooltip
# button), this would silently grab the wrong text as the value. This
# pattern is the sanity check that catches that: the grabbed text has
# to actually LOOK like a currency amount before it's trusted.
FEE_VALUE_PATTERN = re.compile(r"^\d+(?:[.,]\d+)?\s*EGP$", re.IGNORECASE)


def extract_fee_summary(all_labels: list[str]) -> dict[str, str]:
    # Mobile's fee labels are colon-less ("Service Fees"); Internet's
    # bill-flow uses a trailing colon ("Service Fees:") for the same
    # thing. Match against a colon-stripped copy so both work, but keep
    # pulling the VALUE from the original list, one position over.
    normalized = [label.rstrip(":").strip() if label else label for label in all_labels]

    summary = {}
    for key in ("Service Value", "Service Fees", "Total Amount"):
        if key in normalized:
            index = normalized.index(key)
            if index + 1 < len(all_labels):
                candidate_value = all_labels[index + 1]
                stripped = (candidate_value or "").strip()
                if FEE_VALUE_PATTERN.match(stripped):
                    summary[key] = candidate_value
                else:
                    print(f"extract_fee_summary: value after {key!r} doesn't look "
                          f"like a currency amount ({candidate_value!r}) - skipping "
                          f"this key rather than trusting it as a real fee.")
    return summary


def navigate(goal: str, phone_number: str, max_steps: int = 12, tiers=None):
    if not ensure_sahl_open():
        print("Could not get Sahl open from the Android launcher - stopping.")
        return None

    previous_screen_signature = None
    dead_ends_on_this_screen: list[str] = []
    last_choice = None
    retries_used_on_last_choice = 0

    # separate memory just for the form screen - "have I already tried
    # to fill in and confirm THIS exact form state before?"
    previous_form_signature = None
    form_attempt_count = 0

    for step in range(1, max_steps + 1):
        print(f"\n--- Step {step} ---")
        xml_path = dump_current_screen()
        elements = list(list_interesting_elements(xml_path))
        all_labels = [e["label"] for e in elements if e["label"]]

        # session recovery - checked FIRST, before anything else, since
        # this can interrupt the flow at any point, not just one screen.
        # "Ok" and "Sign in" here come from a separate, narrower allowlist
        # (see guardrail.RECOVERY_ALLOWED_LABELS) - never exposed to the
        # AI's free choice, only ever tapped by these exact, tightly
        # scoped conditions.
        if SESSION_EXPIRED_MARKER in all_labels:
            print("Session expired dialog detected - dismissing it.")
            safe_tap_recovery("Ok")
            time.sleep(1.5)
            continue

        if SIGN_IN_SCREEN_MARKER in all_labels:
            print("Landed on the sign-in screen - phone number and password are")
            print("already saved in the app, tapping Sign in.")
            safe_tap_recovery("Sign in")
            time.sleep(2.5)
            continue

        if FEE_SCREEN_MARKER in all_labels:
            print("Reached the fee screen. Stopping here - never tapping Pay.")
            summary = extract_fee_summary(all_labels)
            for key, value in summary.items():
                print(f"  {key}: {value}")
            return summary

        if FORM_SCREEN_MARKER in all_labels:
            form_signature = tuple(all_labels)

            if form_signature == previous_form_signature:
                form_attempt_count += 1
            else:
                previous_form_signature = form_signature
                form_attempt_count = 1

            if form_attempt_count > MAX_FORM_ATTEMPTS:
                print(f"Still on the exact same form state after {form_attempt_count} "
                      f"attempts to make progress on it.")
                print("Something isn't registering - stopping here instead of")
                print("repeating the same thing forever.")
                return None

            phone_already_filled = phone_number in all_labels

            if not phone_already_filled:
                print(f"Phone number not filled in yet - filling it in now "
                      f"(attempt {form_attempt_count}).")
                filled = clear_and_type_field(phone_number)
                if not filled:
                    print("Could not fill in the phone number field - stopping.")
                    return None
                time.sleep(1.0)
                print("Dismissing the keyboard so it doesn't cover anything below it.")
                dismiss_keyboard()
                time.sleep(0.5)
                continue  # re-dump fresh before deciding what's next

            service_section_visible = SERVICE_SECTION_MARKER in all_labels

            if not service_section_visible:
                print(f"Phone number already filled in - now picking the operator "
                      f"(attempt {form_attempt_count}).")
                operator_candidates = sorted({
                    e["label"] for e in elements
                    if e["clickable"] and e["label"] in OPERATOR_LABELS
                })
                if not operator_candidates:
                    print("Phone number is filled in, but no operator options are "
                          "visible - stopping.")
                    return None

                print(f"Operator options here: {operator_candidates}")
                operator_choice = ask_ai_which_label(goal, operator_candidates)
                if operator_choice is None:
                    print("AI did not return a valid operator - stopping.")
                    return None

                print(f"AI picked operator: {operator_choice!r}")
                safe_tap(operator_choice)
                time.sleep(1.5)
                continue  # re-dump fresh - the service section should now appear

            # phone filled AND operator already chosen (the service section
            # only shows up after that) - pick the service and confirm
            print(f"Operator already chosen - now picking the service "
                  f"(attempt {form_attempt_count}).")
            service_candidates = sorted({
                e["label"] for e in elements
                if e["clickable"]
                and e["label"] in ALLOWED_LABELS
                and e["label"] not in OPERATOR_LABELS
                and e["label"] != "Confirm"
            })
            if not service_candidates:
                print("Service list is showing, but none of the visible services are")
                print("on the allowlist yet - stopping rather than guessing. Add the")
                print("one you want (e.g. 'Top-Up') to ALLOWED_LABELS in guardrail.py")
                print("if it's safe.")
                return None

            print(f"Service options here: {service_candidates}")
            service_choice = ask_ai_which_label(goal, service_candidates)
            if service_choice is None:
                print("AI did not return a valid service - stopping.")
                return None

            print(f"AI picked service: {service_choice!r}")
            safe_tap(service_choice)
            time.sleep(1.5)

            print("Tapping Confirm.")
            safe_tap("Confirm")
            print("Waiting for the next screen to finish loading (this often")
            print("triggers a network call for available packages/amounts)...")
            time.sleep(3.0)

            # we just jumped to a new screen out-of-band - reset tracking
            previous_screen_signature = None
            dead_ends_on_this_screen = []
            last_choice = None
            continue

        screen_signature = tuple(all_labels)
        screen_unchanged = screen_signature == previous_screen_signature

        if screen_unchanged and last_choice is not None:
            if retries_used_on_last_choice < MAX_RETRIES_BEFORE_DEAD_END:
                retries_used_on_last_choice += 1
                print(f"{last_choice!r} didn't change anything - retrying it once.")
                safe_tap(last_choice)
                time.sleep(2.5)
                continue

            print(f"{last_choice!r} still did nothing after a retry - "
                  f"treating it as a real dead end here.")
            dead_ends_on_this_screen.append(last_choice)
        elif not screen_unchanged:
            dead_ends_on_this_screen = []

        previous_screen_signature = screen_signature
        retries_used_on_last_choice = 0

        candidates = sorted({
            e["label"] for e in elements
            if e["clickable"] and e["label"] in ALLOWED_LABELS
        } - set(dead_ends_on_this_screen))

        if not candidates:
            editable_fields = [
                e for e in elements if e["class"] == "android.widget.EditText"
            ]

            if editable_fields:
                print("No approved buttons here, but there are input fields on")
                print("screen - this looks like the amount-entry screen. Handing")
                print("off to the fee-tier sweep now...")
                # imported here, not at module top, to avoid a circular import -
                # fee_tiers.py itself imports extract_fee_summary from this file
                from fee_tiers import collect_fee_tiers
                if tiers is not None:
                    return collect_fee_tiers(tiers)
                return collect_fee_tiers()

            print("No approved, tappable, not-already-failed option on this screen.")
            print("This is probably a text field waiting for input - fill it in by")
            print("hand on the emulator, then re-run this script to continue.")
            return None

        # one AI call that both classifies the screen (structural reasoning
        # over everything visible - survives label renames/reordering) and
        # picks the next action, but ONLY from `candidates` - the AI never
        # sees the option to pick anything outside the guardrail's allowlist
        print(f"Safe options here: {candidates}")
        classification = classify_screen_and_pick_action(goal, all_labels, candidates)
        print(f"AI's read on this screen: {classification['screen_type']!r}")
        choice = classification["action"]

        if choice is None:
            print("AI did not return one of the given options - stopping.")
            return None

        print(f"AI picked: {choice!r}")
        last_choice = choice
        safe_tap(choice)
        time.sleep(1.5)

    print("Ran out of steps without reaching the fee screen.")
    return None


if __name__ == "__main__":
    goal = (
        "Navigate to the Mobile Top-Up flow for the Vodafone operator, "
        "so we can see the Service Fees for a top-up."
    )
    TEST_PHONE_NUMBER = "01111198404"
    try:
        results = navigate(goal, phone_number=TEST_PHONE_NUMBER)
    except AdbConnectionError as e:
        print(f"\nadb connection lost - stopping this run. ({e})")
        print("(Future: automatic emulator recovery goes here.)")
        results = None

    print("\n=== Final Results ===")
    if isinstance(results, list):
        for row in results:
            print(row)
    elif isinstance(results, dict):
        print(results)
    else:
        print("No results - navigation did not complete successfully.")
