"""
Step 7: navigate Sahl's "Pay Bill" flow - a different shape from
Mobile Top-Up, confirmed by hand for Internet:

  Home -> tap the service tile (e.g. "Internet")
  -> "Select service type" screen: radio options (Pay Bill / Top-Up /
     Extra Packages), with Pay Bill pre-selected by default - we tap
     it explicitly anyway rather than rely on the default, then tap
     Proceed
  -> Pay Bill form: an account/phone number field (pre-filled with a
     saved default - checked against the intended account_number and
     re-typed if it doesn't match, see _account_number_needs_fix())
     and a "Network Operator" field that opens a company-picker dropdown
     (WE / Etisalat / Orange / Nour, for Internet) - tap it open, pick
     the company, tap Proceed inside the dropdown, then tap Enquire
  -> Enquiry screen: the due amount in a single editable EGP field,
     with Service Value / Service Fees / Total Amount computed live
     underneath - same live-recompute behavior as Mobile's amount
     screen, but ONE field here instead of two. Unlike Mobile's flat
     2.5 EGP fee, Internet's fee here scales with the amount (roughly
     4.6% on the default due amount), so sweeping different typed
     amounts is worth doing here too - see collect_bill_fee_tiers().

We never tap "Pay X EGP" or "Pay All" - guardrail.py's DENY_PATTERN
blocks both no matter what. The one deliberate, hand-verified
exception is "Pay Bill" itself, which is a category radio button, not
a checkout action - see BILL_CATEGORY_DENY_OVERRIDE in guardrail.py
for the reasoning.

Internet's Pay Bill flow has been walked and verified by hand. Landline
has now been confirmed to follow the exact same shape (tile -> Select
service type -> Pay Bill -> Proceed -> company picker -> Enquire),
with "WE" as its operator - see navigate_landline_bill() below.

Electricity/Water/Gas show the same home-screen tile grid and
plausibly share this same shape too, but that's still a guess, not a
confirmation - walk one by hand first (same as every other flow in
this project) before pointing navigate_pay_bill() at it, and add its
tile label + operator/company names to guardrail.ALLOWED_LABELS only
once you've seen them for real.
"""

import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from dump_screen import dump_screen_until_labeled, AdbConnectionError
from launch_sahl import ensure_sahl_open
from tap import (
    safe_tap,
    safe_tap_recovery,
    safe_tap_session_expired_dialog,
    safe_tap_bill_category,
    safe_tap_by_prefix,
    safe_tap_first_present,
    fill_sign_in_form,
    ai_free_choice_tap,
    ai_free_choice_type,
    dismiss_keyboard,
    clear_and_type_field,
)
from navigate import extract_fee_summary, SIGN_IN_SCREEN_MARKER
from ai_pick import suggest_relabel, classify_screen_and_pick_action, ask_ai_free_choice
from guardrail import ALLOWED_LABELS, ACTION_SYNONYMS, SESSION_EXPIRED_MARKER_TEXT, OTP_SCREEN_MARKER
from slack_otp import post_slack_alert, poll_for_otp_code, SlackNotConfigured

load_dotenv()

# The account's own login phone number - a fixed credential, not a
# per-call flow parameter like service_tile_label/operator below. The
# password is deliberately NOT hardcoded here - add SAHL_PASSWORD to
# .env yourself (same style as the existing GEMINI_API_KEY line):
#   SAHL_PASSWORD = "your_actual_password"
SIGN_IN_PHONE_NUMBER = "01111198404"
SAHL_PASSWORD = os.environ.get("SAHL_PASSWORD")

SERVICE_TYPE_MARKER = "Select service type"
PAY_BILL_OPTION = "Pay Bill"
PROCEED = "Proceed"
ENQUIRE = "Enquire"
CHOOSE_COMPANY_MARKER = "Choose Company"
NETWORK_OPERATOR_PREFIX = "Network Operator"
FEE_SCREEN_KEYS = ("Service Value", "Service Fees", "Total Amount")

# The closed set of states determine_bill_screen_state() can return.
# Also doubles as the taxonomy handed to the AI fallback classifier
# (plus "unknown") so there's exactly one place that names these states.
BILL_SCREEN_STATES = [
    "service-type-select",
    "company-picker",
    "operator-dropdown-needed",
    "account-form-ready",
    "fee-confirmation",
]

DEFAULT_TIERS = [100,300,500]
WAIT_FOR_LIVE_CALC_SECONDS = 0.5
# Bumped from 12: session recovery (dismiss dialog + fill sign-in form
# + up to MAX_SIGN_IN_RETRIES wait-retries) can now eat 5+ steps on its
# own before the actual bill flow even starts.
MAX_STEPS = 20
MAX_SIGN_IN_RETRIES = 3
# Without a dedicated cap, a value that never ends up matching (e.g. the
# app silently reformats what you type) would silently burn the whole
# shared MAX_STEPS budget with no specific diagnostic, since the
# account-number-fix branch always `continue`s before reaching the
# loop's own "stuck" detector at the bottom - same reasoning as
# navigate.py's MAX_FORM_ATTEMPTS.
MAX_ACCOUNT_NUMBER_ATTEMPTS = 2


def _labels(elements):
    return [e["label"] for e in elements if e["label"]]


def _looks_like_fee_screen(all_labels):
    normalized = {label.rstrip(":").strip() for label in all_labels if label}
    return any(key in normalized for key in FEE_SCREEN_KEYS)


def _account_number_needs_fix(elements, account_number: str) -> bool:
    """True only when there's exactly one EditText on screen and its
    current value doesn't already match account_number. Zero or
    multiple EditText fields return False ("nothing to fix here") -
    this deliberately never guesses which field is the account number;
    clear_and_type_field's own single-field refusal logic is a second,
    independent line of defense if this is ever reached ambiguously."""
    fields = [e for e in elements if e["class"] == "android.widget.EditText"]
    return len(fields) == 1 and fields[0]["label"] != account_number


def determine_bill_screen_state(all_labels: list[str], operator_confirmed: bool) -> str | None:
    """Pure function: given what's currently visible, plus the one
    piece of state the screen itself can't reveal (operator_confirmed
    - see navigate_pay_bill's docstring for why), figure out which
    known state of the bill flow this is. Returns one of
    BILL_SCREEN_STATES, or None if nothing recognized (caller should
    fall back to the AI classifier).

    Evaluates EVERY condition rather than stopping at the first match,
    and treats more than one matching at once as a bug in the state
    definitions themselves - not something to silently pick a winner
    for by code order. That's deliberate: silently taking "whichever
    branch happened to come first" is exactly the kind of bug that's
    painful to trace after the fact; raising immediately, with the
    exact labels that caused it, is not.

    operator_confirmed is what keeps "operator-dropdown-needed" and
    "account-form-ready" mutually exclusive even though both can be
    visible on screen at once (the Network Operator field shows its
    pre-set default AND the submit button can already be present,
    before we've explicitly re-confirmed the operator this run).

    The company-picker dropdown is a modal overlay (see the "Scrim"
    element that appears alongside it) - the underlying form's own
    labels (the Network Operator field, the submit button) can still
    be present in the tree while it's open, just visually covered. So
    both underlying-form states are also gated on the dropdown NOT
    being open, confirmed against a real run: ['Network Operator',
    'Choose Company', 'WE', 'Proceed', 'Scrim'] was reported as both
    "company-picker" and "operator-dropdown-needed" before this guard
    was added - see bill_state_test.py."""

    matches = []

    company_picker_open = CHOOSE_COMPANY_MARKER in all_labels

    if SERVICE_TYPE_MARKER in all_labels:
        matches.append("service-type-select")

    if company_picker_open:
        matches.append("company-picker")

    network_operator_visible = any(
        label.startswith(NETWORK_OPERATOR_PREFIX) for label in all_labels
    )
    if not company_picker_open and not operator_confirmed and network_operator_visible:
        matches.append("operator-dropdown-needed")

    submit_button_visible = bool(ACTION_SYNONYMS["submit_bill_form"] & set(all_labels))
    if not company_picker_open and operator_confirmed and submit_button_visible:
        matches.append("account-form-ready")

    if _looks_like_fee_screen(all_labels):
        matches.append("fee-confirmation")

    if len(matches) > 1:
        raise RuntimeError(
            f"Ambiguous bill-screen state: more than one state matched at once "
            f"({matches}). This means two state conditions in "
            f"determine_bill_screen_state overlap and need to be made mutually "
            f"exclusive - fix the function, don't silently pick one. "
            f"Labels seen: {all_labels}"
        )

    return matches[0] if matches else None


class SessionRecoveryFailed(Exception):
    """Raised by attempt_session_recovery when recovery itself could not
    be completed (SAHL_PASSWORD missing, the sign-in form couldn't be
    filled even after a retry, or still stuck on the sign-in screen
    after MAX_SIGN_IN_RETRIES). Callers decide what "give up" means in
    their own context - navigate_pay_bill stops the whole run,
    collect_bill_fee_tiers skips just the one tier being measured."""


def attempt_session_recovery(all_labels: list[str], sign_in_state: dict) -> bool:
    """Checks for and handles the two session-recovery conditions
    shared by navigate_pay_bill's main loop and collect_bill_fee_tiers'
    tier sweep: the merged "Session Expired" dialog, and the sign-in
    screen. Returns True if a recovery action (or a retry-wait) was
    taken - the caller should treat the screen as having just changed
    and re-dump/re-check from the top rather than proceeding with the
    all_labels it already has. Returns False if neither condition
    applies - there's nothing to recover from, proceed normally.

    Raises SessionRecoveryFailed if recovery could not be completed -
    see that exception's docstring for what each caller does with it.

    sign_in_state is a small mutable dict the CALLER owns and passes in
    each time - {"attempted": bool, "retry_count": int} - keeps this
    function itself stateless while still tracking progress across a
    caller's own loop iterations. Give each independent loop its OWN
    sign_in_state dict, starting fresh: a session expiry detected
    midway through collect_bill_fee_tiers is definitionally a NEW one
    relative to whatever navigate_pay_bill already recovered from to
    get there, so it should start its own attempted/retry tracking at
    zero, not inherit navigate_pay_bill's."""

    if any(label and SESSION_EXPIRED_MARKER_TEXT in label for label in all_labels):
        print("Session expired dialog detected - dismissing it.")
        safe_tap_session_expired_dialog()
        # a fresh expiry always means a fresh sign-in attempt is needed,
        # even if this caller had already signed in successfully before
        sign_in_state["attempted"] = False
        sign_in_state["retry_count"] = 0
        time.sleep(1.5)
        return True

    if SIGN_IN_SCREEN_MARKER not in all_labels:
        return False

    if sign_in_state["attempted"]:
        # Already filled and submitted the form once but we're STILL
        # seeing the sign-in screen - most likely just a slow
        # transition after a real login (a mid-transition dump caught a
        # few hundred ms after tapping Sign in briefly reported ZERO
        # EditText fields while the screen was navigating away, before
        # this tracking existed), not a reason to re-fill. Wait and
        # re-check instead.
        if sign_in_state["retry_count"] >= MAX_SIGN_IN_RETRIES:
            raise SessionRecoveryFailed(
                f"still on the sign-in screen after {MAX_SIGN_IN_RETRIES} retries "
                f"post-submit - check SAHL_PASSWORD in .env, or whether sign-in is "
                f"failing for some other reason"
            )
        sign_in_state["retry_count"] += 1
        print(f"Still on the sign-in screen after submitting it - waiting to see "
              f"if this is just a slow transition (retry "
              f"{sign_in_state['retry_count']}/{MAX_SIGN_IN_RETRIES}).")
        time.sleep(3.0)
        return True

    if SAHL_PASSWORD is None:
        raise SessionRecoveryFailed(
            "SAHL_PASSWORD is not set in .env - can't fill in the sign-in form"
        )

    print("Landed on the sign-in screen - filling in phone number and password.")
    filled = fill_sign_in_form(SIGN_IN_PHONE_NUMBER, SAHL_PASSWORD)
    if not filled:
        print("Could not fill in the sign-in form - retrying once after a short wait.")
        time.sleep(2.0)
        filled = fill_sign_in_form(SIGN_IN_PHONE_NUMBER, SAHL_PASSWORD)
    if not filled:
        raise SessionRecoveryFailed("could not fill in the sign-in form, even after a retry")

    time.sleep(0.5)
    print("Tapping Sign in.")
    safe_tap_recovery("Sign in")
    sign_in_state["attempted"] = True
    print("Waiting for sign-in to finish loading...")
    time.sleep(4.0)
    return True


class OtpRecoveryFailed(Exception):
    """Raised by attempt_otp_recovery when the OTP screen could not be
    cleared - Slack isn't configured (see slack_otp.SlackNotConfigured),
    no code arrived from Slack within the poll timeout, or the code
    field couldn't be uniquely identified. Same contract as
    SessionRecoveryFailed: callers decide what "give up" means in their
    own context."""


def attempt_otp_recovery(all_labels: list[str]) -> bool:
    """Checks for Sahl's mobile-verification (OTP) screen - see
    guardrail.OTP_SCREEN_MARKER. If present: posts a Slack alert,
    polls the same channel for a reply containing a 6-digit code (see
    slack_otp.py - the code itself comes from a human relaying an
    incoming SMS via an iPhone Shortcuts automation, not anything Sahl
    sends to Slack directly), types it into the code field, and taps
    Verify. Returns True if handled - the caller should treat the
    screen as having just changed and re-dump/re-check from the top,
    same as attempt_session_recovery. Returns False if the OTP screen
    isn't present at all - nothing to recover from, proceed normally.

    Raises OtpRecoveryFailed if the screen couldn't be cleared - see
    that exception's docstring for what each caller does with it."""

    if OTP_SCREEN_MARKER not in all_labels:
        return False

    print("Mobile verification (OTP) screen detected - alerting Slack and waiting for a code.")
    try:
        # timespec="seconds" is load-bearing, not cosmetic: a full
        # isoformat() renders microseconds as exactly six digits, which
        # slack_otp.OTP_CODE_PATTERN matches - so the alert would
        # contain a "code" of its own. poll_for_otp_code already skips
        # this message by ts; keeping a matchable number out of the
        # text is the second, independent check (same discipline
        # guardrail.py applies to taps).
        alert_ts = post_slack_alert(
            f"Sahl bill-pay run needs an OTP code to continue "
            f"(detected at {datetime.now().isoformat(timespec='seconds')})."
        )
    except SlackNotConfigured as e:
        raise OtpRecoveryFailed(f"can't alert Slack: {e}") from e

    code = poll_for_otp_code(alert_ts)
    if code is None:
        raise OtpRecoveryFailed("no OTP code arrived from Slack within the poll timeout")

    xml_path, elements = dump_screen_until_labeled()
    fields = [e for e in elements if e["class"] == "android.widget.EditText"]
    if len(fields) != 1:
        raise OtpRecoveryFailed(
            f"expected exactly one OTP EditText field, found {len(fields)} - refusing to guess"
        )

    filled = clear_and_type_field(code, xml_path=xml_path)
    if not filled:
        raise OtpRecoveryFailed("could not type the OTP code into the field")

    time.sleep(0.5)
    dismiss_keyboard()
    time.sleep(0.5)
    print("Tapping Verify.")
    safe_tap("Verify")
    time.sleep(2.0)
    return True


def navigate_pay_bill(
    service_tile_label: str,
    operator: str,
    account_number: str,
    tiers=None,
    max_steps: int = MAX_STEPS,
    known_values: dict | None = None,
):
    """service_tile_label: the home-screen tile to tap first (e.g.
    "Internet"). operator: which company to pick in the Network
    Operator dropdown (e.g. "WE"). Both must already be verified and on
    guardrail.ALLOWED_LABELS - this function only taps labels that are
    already approved, it doesn't add any.

    account_number: the value the account/phone-number field on the Pay
    Bill form should show. The field comes pre-filled with some saved
    default - this is checked against it every step and re-typed if it
    doesn't match (see _account_number_needs_fix()), rather than
    trusting the default is always correct.

    known_values: extra task-relevant values the free-choice AI fallback
    tier (see below) may type into a field it judges relevant - always
    merged with {"service_tile_label": ..., "operator": ...,
    "account_number": ...}. NEVER put SAHL_PASSWORD's value in here -
    asserted below, not just documented."""

    if known_values is None:
        known_values = {}
    known_values = {
        "service_tile_label": service_tile_label,
        "operator": operator,
        "account_number": account_number,
        **known_values,
    }
    if SAHL_PASSWORD is not None:
        assert SAHL_PASSWORD not in known_values.values(), (
            "known_values must never include SAHL_PASSWORD's value - the free-choice "
            "AI fallback tier has no typing access to the password under any "
            "circumstance; use fill_sign_in_form for that field instead."
        )

    if not ensure_sahl_open():
        print("Could not get Sahl open from the Android launcher - stopping.")
        return None

    previous_signature = None
    # the Network Operator field shows a label like "Network
    # Operator\nWE" EVEN BEFORE we've touched it - WE is a pre-set
    # default, same as the phone number field. So the screen alone
    # can't tell "already confirmed this run" from "still showing the
    # untouched default" - we have to track it ourselves, or the loop
    # just reopens the dropdown, re-picks the same operator, and
    # closes it forever without ever reaching Enquire.
    operator_confirmed = False

    # Whether the home-screen tile has been tapped yet THIS RUN - can't
    # gate this on "step == 1" the way it used to, since
    # attempt_session_recovery/attempt_otp_recovery are checked before
    # this branch and each `continue` on a recovery action consumes a
    # step of its own. A session expiry or a slow sign-in redirect can
    # eat step 1 (or more) entirely before the home screen with the
    # tile ever actually appears - confirmed live: a real run's sign-in
    # recovery consumed step 1, so the tile didn't show up until step
    # 2, and "step == 1" silently never matched, falling through to the
    # tier-2 AI fallback for something that should've been deterministic.
    home_tile_tapped = False

    # Counts how many times the account-number-fix branch below has
    # fired this run - see MAX_ACCOUNT_NUMBER_ATTEMPTS.
    account_number_attempts = 0

    # See attempt_session_recovery's docstring for what this tracks and
    # why it has to be caller-owned rather than hidden inside that
    # function.
    sign_in_state = {"attempted": False, "retry_count": 0}

    # Gates the free-choice AI fallback tier (tier 3, see below): once
    # we've ever reached the fee/enquiry screen this run, that tier is
    # permanently disabled for the rest of the run - confines it to the
    # lower-stakes navigational part of the flow, keeps everything at
    # and after the fee screen exactly as strict as it's always been.
    fee_confirmation_reached = False

    for step in range(1, max_steps + 1):
        print(f"\n--- Bill-flow step {step} ---")
        # dump_screen_until_labeled instead of a plain dump: waits out
        # loading transitions (network calls after Enquire/submit, sign-
        # in redirects, etc.) instead of catching a blank screen and
        # falling straight through to "unrecognized" - see its docstring.
        xml_path, elements = dump_screen_until_labeled()
        all_labels = _labels(elements)

        # session recovery - checked FIRST, before anything else, since
        # this can interrupt the flow at any point, not just one screen
        # (same reasoning as navigate.py's identical check).
        try:
            if attempt_session_recovery(all_labels, sign_in_state):
                continue
        except SessionRecoveryFailed as e:
            print(f"Session recovery failed - stopping. ({e})")
            return None

        try:
            if attempt_otp_recovery(all_labels):
                continue
        except OtpRecoveryFailed as e:
            print(f"OTP recovery failed - stopping. ({e})")
            return None

        # 1. home screen - tap the service tile (only makes sense once,
        #    before we've ever tapped it this run - see home_tile_tapped
        #    above for why this can no longer be gated on step == 1).
        #    If it's not there, we don't special-case anything here -
        #    just fall through to the catch-all at the bottom, which
        #    already checks service_tile_label (and every other known
        #    anchor) via suggest_relabel before giving up.
        if not home_tile_tapped and service_tile_label in all_labels:
            print(f"Tapping the {service_tile_label!r} tile on the home screen.")
            safe_tap(service_tile_label, xml_path)
            home_tile_tapped = True
            time.sleep(1.5)
            continue

        state = determine_bill_screen_state(all_labels, operator_confirmed)

        # 1.5 Pay Bill form's account/phone-number field - checked
        #     whenever the underlying form is visible with the dropdown
        #     CLOSED (the field is covered by a modal Scrim while
        #     "company-picker" is open, so this deliberately excludes
        #     that state - see determine_bill_screen_state's docstring).
        #     Logs which branch fired every time, on purpose - this is
        #     the one spot a silent no-op ("already correct" vs "never
        #     even checked") is easy to mistake for the other from the
        #     console output alone.
        if state in ("operator-dropdown-needed", "account-form-ready"):
            if not _account_number_needs_fix(elements, account_number):
                print(f"Account number field already shows {account_number!r} - leaving it alone.")
            else:
                account_number_attempts += 1
                if account_number_attempts > MAX_ACCOUNT_NUMBER_ATTEMPTS:
                    print(f"Account number field still doesn't show {account_number!r} "
                          f"after {account_number_attempts - 1} attempts to fix it - "
                          f"stopping rather than repeating the same thing forever.")
                    return None
                print(f"Account number field doesn't show {account_number!r} yet - "
                      f"filling it in (attempt {account_number_attempts}).")
                filled = clear_and_type_field(account_number, xml_path=xml_path)
                if not filled:
                    print("Could not fill in the account number field - stopping.")
                    return None
                time.sleep(1.0)
                dismiss_keyboard()
                time.sleep(0.5)
                continue

        # 2. "Select service type" screen - choose Pay Bill (the one
        #    hand-verified exception to the deny pattern), then Proceed
        if state == "service-type-select":
            if PAY_BILL_OPTION in all_labels:
                print("Selecting 'Pay Bill'.")
                safe_tap_bill_category(PAY_BILL_OPTION, xml_path)
                time.sleep(1.0)
            if PROCEED in all_labels:
                print("Tapping Proceed.")
                safe_tap(PROCEED, xml_path)
                time.sleep(1.5)
                continue
            print("On the service-type screen but can't find Pay Bill/Proceed - stopping.")
            return None

        # 3a. company picker is open - pick the operator, then Proceed
        if state == "company-picker":
            if operator in all_labels:
                print(f"Picking operator {operator!r} from the dropdown.")
                safe_tap(operator, xml_path)
                time.sleep(1.0)
            if PROCEED in all_labels:
                print("Tapping Proceed to close the dropdown.")
                safe_tap(PROCEED, xml_path)
                operator_confirmed = True
                time.sleep(1.0)
                continue
            print("Company picker is open but can't find the operator/Proceed - stopping.")
            return None

        # 3b. Pay Bill form, operator not confirmed YET THIS RUN - open
        #     the dropdown. Once operator_confirmed is True this state
        #     won't fire again even though the field's label still
        #     shows the same text - see determine_bill_screen_state.
        if state == "operator-dropdown-needed":
            print("Opening the Network Operator dropdown.")
            opened = safe_tap_by_prefix(NETWORK_OPERATOR_PREFIX, xml_path)
            if not opened:
                return None
            time.sleep(1.0)
            continue

        # 3c. Pay Bill form, operator already confirmed - submit it.
        # Internet's form uses "Enquire" here; Landline's uses "Proceed"
        # again instead (confirmed by hand) - safe_tap_first_present
        # taps whichever already-approved member of the
        # "submit_bill_form" synonym group is actually visible, instead
        # of the code hardcoding one specific label per service.
        if state == "account-form-ready":
            print("Submitting the bill form.")
            if not safe_tap_first_present("submit_bill_form", xml_path):
                print("Could not submit the bill form - stopping.")
                return None
            time.sleep(2.0)
            continue

        # 4. Enquiry / amount screen
        if state == "fee-confirmation":
            # Set for defensive correctness / future-proofing: as this
            # function is currently written, reaching this branch always
            # returns immediately below, so tier 3 (further down) can
            # never actually observe this flag True within THIS
            # function's own loop today. It's still set here rather than
            # relied on being unreachable, so it stays correct if a
            # future change ever makes this branch NOT return right away.
            fee_confirmation_reached = True
            editable_fields = [e for e in elements if e["class"] == "android.widget.EditText"]
            if editable_fields:
                print("Reached the amount/fee screen - sweeping tiers now.")
                return collect_bill_fee_tiers(tiers or DEFAULT_TIERS)
            print("Reached a fee screen with no editable amount - reading it as-is.")
            return extract_fee_summary(all_labels)

        # Nothing in the deterministic table matched (state is None) -
        # tier 2: ask the AI to classify the FULL screen and pick from
        # the SAME allowlist-filtered candidates the deterministic path
        # would have used. This can never expand what's tappable -
        # `action` is still verified against `candidates` inside
        # classify_screen_and_pick_action, so a wrong or hallucinated
        # answer degrades to "nothing picked", never to an unapproved tap.
        print("[FALLBACK] No known deterministic state matched here - asking the AI.")
        candidates = sorted({
            e["label"] for e in elements
            if e["clickable"] and e["label"] in ALLOWED_LABELS
        })
        goal = (
            f"Continue the {service_tile_label} Pay Bill flow (operator "
            f"{operator!r}) toward the fee/enquiry screen."
        )
        classification = classify_screen_and_pick_action(
            goal, all_labels, candidates, screen_types=BILL_SCREEN_STATES + ["unknown"]
        )
        print(f"[FALLBACK] AI's read on this screen: {classification['screen_type']!r}")
        action = classification["action"]

        if action is not None:
            print(f"[FALLBACK] AI picked: {action!r}")
            safe_tap(action)
            time.sleep(1.5)
            continue

        # tier 3: free-choice AI fallback - only reached if tier 2 found
        # nothing, AND only while fee_confirmation_reached is still
        # False (see its comment above and navigate_bill.py's module
        # docstring for the full safety reasoning). Can tap OR type
        # anything currently visible, with no allowlist/deny-pattern
        # check - an explicit, informed exception, not a default.
        if not fee_confirmation_reached:
            print("[FREE-CHOICE] Allowlist-constrained tiers found nothing - "
                  "asking the AI to choose freely from the whole screen.")
            free_choice = ask_ai_free_choice(goal, all_labels, elements, known_values)
            print(f"[FREE-CHOICE] AI's choice: {free_choice}")

            if free_choice["action_type"] == "tap":
                acted = ai_free_choice_tap(
                    free_choice["target_label"], goal=goal, all_labels=all_labels,
                    reasoning=free_choice["reasoning"],
                )
                if acted:
                    time.sleep(1.5)
                    continue
            elif free_choice["action_type"] == "type":
                value = known_values[free_choice["value_key"]]
                acted = ai_free_choice_type(
                    free_choice["target_nearby_label"], free_choice["value_key"], value,
                    goal=goal, all_labels=all_labels, reasoning=free_choice["reasoning"],
                )
                if acted:
                    time.sleep(1.0)
                    continue

        # AI found nothing safe to do either - before giving up, ask it
        # whether any of our known anchors for this flow might just
        # have been renamed (never auto-tapped - see suggest_relabel's
        # docstring, this is a suggestion for a human, not an action).
        known_anchors = [
            service_tile_label, SERVICE_TYPE_MARKER, PAY_BILL_OPTION, PROCEED,
            ENQUIRE, CHOOSE_COMPANY_MARKER, operator, *FEE_SCREEN_KEYS,
        ]
        suggest_relabel(known_anchors, all_labels)

        signature = tuple(all_labels)
        if signature == previous_signature:
            print("Screen didn't change after the last action - stopping rather than looping.")
            print(f"  Labels seen: {all_labels}")
            return None
        previous_signature = signature

        print("Unrecognized screen for the bill flow - stopping here.")
        print(f"  Labels seen: {all_labels}")
        return None

    print("Ran out of steps without reaching the fee screen.")
    return None


def navigate_landline_bill(
    account_number: str, operator: str = "WE", tiers=None, max_steps: int = MAX_STEPS
):
    """Landline's Pay Bill flow - confirmed by hand to be the exact same
    shape as Internet's, just starting from the "Landline" home-screen
    tile instead of "Internet". Thin wrapper around navigate_pay_bill()
    rather than a separate implementation, since nothing about the
    steps themselves differs.

    account_number has to come before operator here (Python requires
    non-default params before defaulted ones) - the reverse of its
    position in navigate_pay_bill itself, which has no such
    constraint."""

    return navigate_pay_bill(
        service_tile_label="Landline", operator=operator, account_number=account_number,
        tiers=tiers, max_steps=max_steps,
    )


MAX_RECOVERY_ATTEMPTS_PER_TIER = MAX_SIGN_IN_RETRIES + 3


def _collect_one_tier(amount, sign_in_state: dict):
    """Fills in one amount, reads back the fee breakdown, and returns
    it as a row dict - or None if this tier should be skipped. Session
    recovery is checked before typing AND before reading the result
    (either point can catch a mid-sweep session expiry - see
    attempt_session_recovery); either way, a successful recovery
    retries this SAME tier from the top rather than silently losing the
    data point, up to MAX_RECOVERY_ATTEMPTS_PER_TIER attempts."""

    for _ in range(MAX_RECOVERY_ATTEMPTS_PER_TIER):
        xml_path, elements = dump_screen_until_labeled()
        all_labels = _labels(elements)
        try:
            if attempt_session_recovery(all_labels, sign_in_state):
                continue  # recovered (or waiting) - re-check before doing anything
        except SessionRecoveryFailed as e:
            print(f"Could not recover session for {amount} EGP - skipping this tier. ({e})")
            return None

        try:
            if attempt_otp_recovery(all_labels):
                continue
        except OtpRecoveryFailed as e:
            print(f"Could not recover OTP for {amount} EGP - skipping this tier. ({e})")
            return None

        filled = clear_and_type_field(str(amount), xml_path=xml_path)
        if not filled:
            print(f"Could not fill in {amount} EGP - skipping this tier.")
            return None
        dismiss_keyboard()
        print("Waiting for the live fee calculation to update...")
        time.sleep(WAIT_FOR_LIVE_CALC_SECONDS)

        xml_path, elements = dump_screen_until_labeled()
        all_labels = _labels(elements)
        try:
            if attempt_session_recovery(all_labels, sign_in_state):
                print(f"Session needed recovery while reading {amount} EGP's "
                      f"result - retrying this tier from the top.")
                continue
        except SessionRecoveryFailed as e:
            print(f"Could not recover session while reading {amount} EGP - "
                  f"skipping this tier. ({e})")
            return None

        try:
            if attempt_otp_recovery(all_labels):
                print(f"OTP needed while reading {amount} EGP's result - "
                      f"retrying this tier from the top.")
                continue
        except OtpRecoveryFailed as e:
            print(f"Could not recover OTP while reading {amount} EGP - "
                  f"skipping this tier. ({e})")
            return None

        summary = extract_fee_summary(all_labels)
        if not summary:
            print(f"No fee breakdown appeared for {amount} EGP - skipping this tier.")
            return None
        return {"Amount Requested": f"{amount} EGP", **summary}

    print(f"Recovery took too many attempts for {amount} EGP - skipping this tier.")
    return None


def collect_bill_fee_tiers(tiers=DEFAULT_TIERS):
    results = []
    # Own local sign_in_state, independent of navigate_pay_bill's - by
    # the time this function runs we're already past the original
    # sign-in, so any expiry detected here is definitionally new and
    # should start attempted/retry tracking fresh, not inherit state
    # from whatever navigate_pay_bill already recovered from.
    sign_in_state = {"attempted": False, "retry_count": 0}

    for amount in tiers:
        print(f"\n--- Testing amount: {amount} EGP ---")
        row = _collect_one_tier(amount, sign_in_state)
        if row is not None:
            results.append(row)
            print(f"  {row}")
    return results


RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def save_results(results, service_tile_label: str, operator: str, account_number: str) -> str:
    """Writes one run's results to results/sahl_fees_<timestamp>.json -
    same directory and "collected_at" + "results" shape as the old
    (deleted) run_all_operators.py's save_results(), so a GitHub
    Actions workflow uploading results/*.json as an artifact actually
    has something to find. Unlike that old multi-operator sweep script,
    `results` here is just whatever this one call to
    navigate_pay_bill()/navigate() returned - not nested per operator.
    Returns the path written."""

    os.makedirs(RESULTS_DIR, exist_ok=True)
    now = datetime.now()
    path = os.path.join(RESULTS_DIR, f"sahl_fees_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json")
    payload = {
        "collected_at": now.isoformat(),
        "service_tile_label": service_tile_label,
        "operator": operator,
        "account_number": account_number,
        "results": results,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


if __name__ == "__main__":
    # AdbConnectionError propagates up through collect_bill_fee_tiers()
    # (called from inside navigate_pay_bill's own call stack) as well as
    # navigate_pay_bill() itself, so this one wrap covers both without
    # needing a separate try/except inside either function.
    # Defaults match the values verified by hand; a GitHub Actions
    # workflow_dispatch run can override any of these via env vars set
    # from its own inputs, without changing the defaults for a plain
    # local `python navigate_bill.py` run.
    SERVICE_TILE_LABEL = os.environ.get("BILL_SERVICE_TILE_LABEL", "Internet")
    OPERATOR = os.environ.get("BILL_OPERATOR", "WE")
    TEST_ACCOUNT_NUMBER = os.environ.get("BILL_ACCOUNT_NUMBER", "0225305397")
    try:
        results = navigate_pay_bill(
            service_tile_label=SERVICE_TILE_LABEL, operator=OPERATOR, account_number=TEST_ACCOUNT_NUMBER,
        )
    except AdbConnectionError as e:
        print(f"\nadb connection lost - stopping this run. ({e})")
        print("(Future: automatic emulator recovery goes here.)")
        results = None

    print(f"\n=== Final Results ({SERVICE_TILE_LABEL}, Pay Bill, {OPERATOR}) ===")
    if isinstance(results, list):
        for row in results:
            print(row)
        saved_path = save_results(results, SERVICE_TILE_LABEL, OPERATOR, TEST_ACCOUNT_NUMBER)
        print(f"Saved to {saved_path}")
    elif isinstance(results, dict):
        print(results)
        saved_path = save_results(results, SERVICE_TILE_LABEL, OPERATOR, TEST_ACCOUNT_NUMBER)
        print(f"Saved to {saved_path}")
    else:
        print("No results - navigation did not complete successfully.")
        sys.exit(1)
