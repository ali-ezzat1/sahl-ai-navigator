"""
Synthetic, emulator-free regression test for determine_bill_screen_state()
in navigate_bill.py - same style as ai_pick.py's own __main__ block and
fill_field_test.py: no test framework, just recorded/hand-crafted label
snapshots run through the real function and checked against what we
expect.

Run directly: python bill_state_test.py

This is meant to be the first thing to run after touching
determine_bill_screen_state, ACTION_SYNONYMS, or navigate_pay_bill's
state-handling branches - it catches state-detection bugs in about a
second, with zero adb/emulator involved, instead of only finding out
mid-run against the real app.
"""

import os
import subprocess

import navigate_bill
from navigate_bill import (
    determine_bill_screen_state,
    attempt_session_recovery,
    SessionRecoveryFailed,
    attempt_otp_recovery,
    OtpRecoveryFailed,
    MAX_SIGN_IN_RETRIES,
    navigate_pay_bill,
    _account_number_needs_fix,
)
from guardrail import check_session_expired_tap_allowed, OTP_SCREEN_MARKER
import dump_screen
from dump_screen import list_interesting_elements, find_editable_fields, run_adb, AdbConnectionError
from navigate import SIGN_IN_SCREEN_MARKER, extract_fee_summary
import ai_pick
from ai_pick import ask_ai_free_choice
import tap
from tap import ai_free_choice_tap, ai_free_choice_type
import slack_otp
from slack_otp import SlackNotConfigured

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "test_fixtures")
SIGN_IN_SCREEN_XML = os.path.join(FIXTURES_DIR, "sign_in_screen.xml")

SESSION_EXPIRED_CASES = [
    # (description, label, expected_allowed)
    (
        "real merged Session Expired/Ok dialog from a hand-captured XML dump",
        "Session Expired\n108 : Your session has expired. Please sign in again to continue\nOk",
        True,
    ),
    (
        "does not contain the marker at all - must refuse",
        "Some Other Dialog\nOk",
        False,
    ),
    (
        "contains the marker but ALSO looks like a payment button - "
        "DENY_PATTERN must still win (defense in depth)",
        "Pay 173.93 EGP Session Expired trick",
        False,
    ),
]

CASES = [
    # (description, all_labels, operator_confirmed, expected_state)
    (
        "service-type screen",
        ["Select service type", "Pay Bill", "Top-Up", "Extra Packages", "Proceed"],
        False,
        "service-type-select",
    ),
    (
        "company picker open",
        ["Choose Company", "WE", "Etisalat", "Orange", "Nour", "Proceed"],
        False,
        "company-picker",
    ),
    (
        "company picker open, REAL crash snapshot: the underlying "
        "Network Operator field is still in the tree (covered by the "
        "Scrim overlay) and must NOT also match operator-dropdown-needed",
        ["Network Operator", "Choose Company", "WE", "Proceed", "Scrim"],
        False,
        "company-picker",
    ),
    (
        "account form, operator NOT yet confirmed this run "
        "(the exact overlap case - Proceed is ALSO visible here, "
        "but operator_confirmed=False must still win)",
        ["0225305397", "Network Operator\nWE", "Landline", "Proceed"],
        False,
        "operator-dropdown-needed",
    ),
    (
        "account form, operator already confirmed - real Landline snapshot "
        "from the run that broke before this fix (Proceed, not Enquire)",
        ["0225305397", "Network Operator\nWE", "Landline", "Proceed"],
        True,
        "account-form-ready",
    ),
    (
        "account form, operator already confirmed - Internet's shape (Enquire)",
        ["01111198404", "Network Operator\nWE", "Internet", "Enquire"],
        True,
        "account-form-ready",
    ),
    (
        "fee/enquiry screen",
        ["Service Value", "142.86 EGP", "Service Fees", "2.5 EGP", "Total Amount", "145.36 EGP"],
        True,
        "fee-confirmation",
    ),
    (
        "fee screen, Internet's colon-suffixed labels",
        ["Service Value:", "428.58 EGP", "Service Fees:", "2.5 EGP", "Total Amount:", "431.08 EGP"],
        True,
        "fee-confirmation",
    ),
    (
        "totally unrecognized screen - must fall back (None), never crash",
        ["Some Brand New Screen We've Never Seen"],
        True,
        None,
    ),
]

AMBIGUOUS_CASES = [
    (
        "contrived overlap: service-type markers AND fee markers at once "
        "- must raise RuntimeError rather than silently picking one",
        ["Select service type", "Service Fees", "2.5 EGP"],
        False,
    ),
]

ACCOUNT_NUMBER_FIX_CASES = [
    # (description, elements, account_number, expected)
    (
        "single EditText already matches - nothing to fix",
        [{"label": "0225305397", "clickable": True, "class": "android.widget.EditText",
          "nearby_label": None, "bounds": "[176,448][810,538]", "is_password": False}],
        "0225305397",
        False,
    ),
    (
        "single EditText shows the wrong value - needs fixing",
        [{"label": "0100000000", "clickable": True, "class": "android.widget.EditText",
          "nearby_label": None, "bounds": "[176,448][810,538]", "is_password": False}],
        "0225305397",
        True,
    ),
    (
        "zero EditText fields - nothing to fix, refuse to guess",
        [{"label": "Network Operator\nWE", "clickable": True, "class": "android.view.View",
          "nearby_label": None, "bounds": "[47,603][1033,750]", "is_password": False}],
        "0225305397",
        False,
    ),
    (
        "two EditText fields - ambiguous, refuse to guess",
        [
            {"label": "0100000000", "clickable": True, "class": "android.widget.EditText",
             "nearby_label": None, "bounds": "[0,0][10,10]", "is_password": False},
            {"label": "9999", "clickable": True, "class": "android.widget.EditText",
             "nearby_label": None, "bounds": "[0,10][10,20]", "is_password": False},
        ],
        "0225305397",
        False,
    ),
]


def run():
    failures = 0

    for description, all_labels, operator_confirmed, expected in CASES:
        actual = determine_bill_screen_state(all_labels, operator_confirmed)
        status = "PASS" if actual == expected else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"{status}  {description}")
        print(f"      expected={expected!r} actual={actual!r}")

    for description, all_labels, operator_confirmed in AMBIGUOUS_CASES:
        try:
            actual = determine_bill_screen_state(all_labels, operator_confirmed)
            print(f"FAIL  {description}")
            print(f"      expected a RuntimeError, got a normal return: {actual!r}")
            failures += 1
        except RuntimeError as e:
            print(f"PASS  {description}")
            print(f"      raised RuntimeError as expected: {e}")

    for description, elements, account_number, expected in ACCOUNT_NUMBER_FIX_CASES:
        actual = _account_number_needs_fix(elements, account_number)
        status = "PASS" if actual == expected else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"{status}  _account_number_needs_fix: {description}")
        print(f"      expected={expected!r} actual={actual!r}")

    # Sign-in screen: parsed from a real recorded XML dump, not a
    # hand-typed snapshot - catches drift in dump_screen.py's own
    # parsing (label/clickable/is_password extraction), not just in
    # navigate_bill.py's logic built on top of it.
    sign_in_elements = list(list_interesting_elements(SIGN_IN_SCREEN_XML))
    sign_in_labels = [e["label"] for e in sign_in_elements if e["label"]]

    checks = [
        (
            "SIGN_IN_SCREEN_MARKER ('Forgot password?') present",
            SIGN_IN_SCREEN_MARKER in sign_in_labels,
        ),
        (
            "exactly one CLICKABLE 'Sign in' element (the button, not "
            "the non-clickable title also named 'Sign in')",
            len([e for e in sign_in_elements
                 if e["label"] == "Sign in" and e["clickable"]]) == 1,
        ),
        (
            "exactly 2 EditText fields on the sign-in form",
            len(find_editable_fields(SIGN_IN_SCREEN_XML)) == 2,
        ),
        (
            "exactly 1 of those fields is flagged is_password=True",
            len([f for f in find_editable_fields(SIGN_IN_SCREEN_XML)
                 if f["is_password"]]) == 1,
        ),
    ]
    for description, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  sign-in screen fixture: {description}")

    # run_adb: every failure mode must surface as AdbConnectionError
    # after retrying ADB_RETRY_ATTEMPTS times (never a raw
    # CalledProcessError/TimeoutExpired), except FileNotFoundError,
    # which must raise immediately with NO retry - that's a permanent
    # "adb isn't on PATH" problem, not a transient one. Monkeypatch
    # subprocess.run and time.sleep temporarily rather than relying on
    # a real adb binary or real delays wherever this test runs.
    real_subprocess_run = dump_screen.subprocess.run
    real_dump_screen_sleep = dump_screen.time.sleep
    dump_screen.time.sleep = lambda seconds: None

    def _raise(exc):
        def _fake_run(*args, **kwargs):
            raise exc
        return _fake_run

    ADB_FAILURE_CASES = [
        (
            "adb exits non-zero every attempt (CalledProcessError) - "
            "retries, then raises AdbConnectionError",
            subprocess.CalledProcessError(1, ["adb", "shell", "true"]),
        ),
        (
            "adb call times out every attempt (TimeoutExpired) - "
            "retries, then raises AdbConnectionError",
            subprocess.TimeoutExpired(["adb", "shell", "true"], 5),
        ),
        (
            "adb itself not found on PATH (FileNotFoundError) - "
            "raises immediately, no retry",
            FileNotFoundError("adb"),
        ),
    ]
    for description, exc in ADB_FAILURE_CASES:
        call_count = {"n": 0}

        def _fake_run(*args, **kwargs):
            call_count["n"] += 1
            raise exc
        dump_screen.subprocess.run = _fake_run
        try:
            run_adb("shell", "true")
            print(f"FAIL  run_adb: {description} - expected AdbConnectionError, got no exception")
            failures += 1
        except AdbConnectionError:
            expected_calls = 1 if isinstance(exc, FileNotFoundError) else dump_screen.ADB_RETRY_ATTEMPTS
            passed = call_count["n"] == expected_calls
            status = "PASS" if passed else "FAIL"
            if not passed:
                failures += 1
            print(f"{status}  run_adb: {description} -> raised AdbConnectionError "
                  f"after {call_count['n']} call(s) (expected {expected_calls})")
        except Exception as e:
            print(f"FAIL  run_adb: {description} - wrong exception type "
                  f"{type(e).__name__}: {e}")
            failures += 1
        finally:
            dump_screen.subprocess.run = real_subprocess_run

    # transient failure - fails once, then succeeds - must recover via
    # retry rather than raising, proving the retry loop actually helps
    # rather than just re-labeling every failure as AdbConnectionError.
    transient_call_count = {"n": 0}

    def _fake_run_transient_recovery(*args, **kwargs):
        transient_call_count["n"] += 1
        if transient_call_count["n"] == 1:
            raise subprocess.CalledProcessError(137, ["adb", "shell", "true"])
        return subprocess.CompletedProcess(args, 0)

    dump_screen.subprocess.run = _fake_run_transient_recovery
    try:
        result = run_adb("shell", "true")
        passed = transient_call_count["n"] == 2 and result.returncode == 0
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  run_adb: transient failure (fails once, then succeeds) - "
              f"retry recovers instead of raising")
    except AdbConnectionError as e:
        print(f"FAIL  run_adb: transient failure - retry should have recovered, "
              f"raised AdbConnectionError instead: {e}")
        failures += 1
    finally:
        dump_screen.subprocess.run = real_subprocess_run
        dump_screen.time.sleep = real_dump_screen_sleep

    # dump_screen_until_labeled: verify the stability mechanism (Item D)
    # actually waits out a screen whose labels are still CHANGING across
    # reads, not just one that's fully empty - mocked so this runs in
    # milliseconds instead of real adb-paced seconds.
    real_dump_current_screen = dump_screen.dump_current_screen
    real_list_interesting_elements = dump_screen.list_interesting_elements

    def _mock_sequence(label_sequence):
        """Each call to dump_current_screen advances one step through
        label_sequence (staying on the last entry once exhausted) -
        list_interesting_elements then just returns that step's labels
        as fake elements, ignoring the xml_path entirely."""
        state = {"i": 0}

        def _fake_dump():
            return state["i"]  # xml_path stands in as a step index here

        def _fake_list(step_index):
            step_index = min(step_index, len(label_sequence) - 1)
            state["i"] += 1
            return [{"label": lbl, "clickable": False} for lbl in label_sequence[step_index]]

        return _fake_dump, _fake_list

    STABILITY_CASES = [
        (
            "already stable on the very first read - should stop "
            "quickly, not wait out the full poll cycle",
            [["X", "Y"], ["X", "Y"], ["X", "Y"]],
            frozenset({"X", "Y"}),
        ),
        (
            "content still CHANGING across reads (screen inflating) "
            "before settling - must NOT stop on the transient labels",
            [[], ["Loading"], ["Loading", "MorePartialContent"], ["Real", "Final", "Content"],
             ["Real", "Final", "Content"]],
            frozenset({"Real", "Final", "Content"}),
        ),
        (
            "never stabilizes within max_wait_seconds - must return "
            "the last read instead of hanging forever",
            [["A"], ["B"], ["C"], ["D"], ["E"], ["F"], ["G"], ["H"], ["I"], ["J"], ["K"]],
            None,  # any single-label frozenset is acceptable - just must return, not hang
        ),
    ]

    for description, label_sequence, expected in STABILITY_CASES:
        fake_dump, fake_list = _mock_sequence(label_sequence)
        dump_screen.dump_current_screen = fake_dump
        dump_screen.list_interesting_elements = fake_list
        try:
            _, result_elements = dump_screen.dump_screen_until_labeled(
                max_wait_seconds=0.05, poll_interval=0.01, stability_check_seconds=0.01
            )
            actual = frozenset(e["label"] for e in result_elements if e["label"])
            passed = actual == expected if expected is not None else True
        finally:
            dump_screen.dump_current_screen = real_dump_current_screen
            dump_screen.list_interesting_elements = real_list_interesting_elements

        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  dump_screen_until_labeled: {description}")
        print(f"      expected={expected!r} actual={actual!r}")

    # A screen carrying a live countdown must NOT defeat the stability
    # check. Sahl's OTP screen ticks "Resend OTP in 35s" down once a
    # second, so two consecutive reads never matched and every visit
    # burned the full max_wait_seconds (~19 wasted dumps, observed
    # live). With VOLATILE_LABEL_PREFIXES filtered out of the
    # comparison, the rest of the screen is stable on read 2.
    countdown_screens = [
        ["Mobile Verification", "Enter the 6 Digit Code", "Verify", f"Resend OTP in {n}s"]
        for n in range(35, 15, -1)  # 20 distinct reads, countdown never repeats
    ]
    countdown_state = {"dumps": 0}

    def _fake_countdown_dump():
        return countdown_state["dumps"]

    def _fake_countdown_list(step_index):
        idx = min(step_index, len(countdown_screens) - 1)
        countdown_state["dumps"] += 1
        return [{"label": lbl, "clickable": False} for lbl in countdown_screens[idx]]

    real_dump_sleep = dump_screen.time.sleep
    dump_screen.dump_current_screen = _fake_countdown_dump
    dump_screen.list_interesting_elements = _fake_countdown_list
    dump_screen.time.sleep = lambda seconds: None
    try:
        _, countdown_elements = dump_screen.dump_screen_until_labeled(max_wait_seconds=10.0)
        returned_labels = {e["label"] for e in countdown_elements if e["label"]}
        # stopped on read 2 (not ~21), and the countdown is still handed
        # to the caller - only the COMPARISON ignores it
        passed = (
            countdown_state["dumps"] == 2
            and any(lbl.startswith("Resend OTP in") for lbl in returned_labels)
        )
    finally:
        dump_screen.dump_current_screen = real_dump_current_screen
        dump_screen.list_interesting_elements = real_list_interesting_elements
        dump_screen.time.sleep = real_dump_sleep

    status = "PASS" if passed else "FAIL"
    if not passed:
        failures += 1
    print(f"{status}  dump_screen_until_labeled: a live countdown label doesn't "
          f"defeat stability detection (stopped after {countdown_state['dumps']} "
          f"dumps, expected 2)")

    # extract_fee_summary: value must actually look like a currency
    # amount before being trusted - real observed values from an actual
    # completed run (results/sahl_fees_2026-08-30_12-01-27.json) must
    # still pass, but a structurally-shifted value must be rejected
    # rather than silently returned as if it were a real fee.
    FEE_SUMMARY_CASES = [
        (
            "real observed values (Vodafone, 100 EGP tier) - must all "
            "come through unchanged",
            ["Service Value", "142.86 EGP", "Service Fees", "2.5 EGP",
             "Total Amount", "145.36 EGP"],
            {"Service Value": "142.86 EGP", "Service Fees": "2.5 EGP",
             "Total Amount": "145.36 EGP"},
        ),
        (
            "Internet's colon-suffixed keys, real observed values (300 EGP tier)",
            ["Service Value:", "428.58 EGP", "Service Fees:", "2.5 EGP",
             "Total Amount:", "431.08 EGP"],
            {"Service Value": "428.58 EGP", "Service Fees": "2.5 EGP",
             "Total Amount": "431.08 EGP"},
        ),
        (
            "a non-currency value shifted into the value position - "
            "must be skipped, not returned as a fake fee",
            ["Service Value", "Some Other Label", "Service Fees", "2.5 EGP"],
            {"Service Fees": "2.5 EGP"},
        ),
    ]
    for description, all_labels, expected in FEE_SUMMARY_CASES:
        actual = extract_fee_summary(all_labels)
        status = "PASS" if actual == expected else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"{status}  extract_fee_summary: {description}")
        print(f"      expected={expected!r} actual={actual!r}")

    # attempt_session_recovery: the stateful decision logic (when to
    # re-fill vs. wait-and-retry vs. give up) mocked so it never touches
    # adb - a call log records what would have been tapped/filled/slept.
    real_safe_tap_session_expired_dialog = navigate_bill.safe_tap_session_expired_dialog
    real_fill_sign_in_form = navigate_bill.fill_sign_in_form
    real_safe_tap_recovery = navigate_bill.safe_tap_recovery
    real_sahl_password = navigate_bill.SAHL_PASSWORD
    real_sleep = navigate_bill.time.sleep

    def _install_recovery_mocks(fill_sign_in_form_result=True, password="fake-password"):
        call_log = []
        navigate_bill.safe_tap_session_expired_dialog = lambda: call_log.append("dismiss_expired")
        navigate_bill.fill_sign_in_form = lambda phone, pw: (
            call_log.append("fill_form") or fill_sign_in_form_result
        )
        navigate_bill.safe_tap_recovery = lambda label: call_log.append(f"tap:{label}")
        navigate_bill.SAHL_PASSWORD = password
        navigate_bill.time.sleep = lambda seconds: None
        return call_log

    def _restore_recovery_mocks():
        navigate_bill.safe_tap_session_expired_dialog = real_safe_tap_session_expired_dialog
        navigate_bill.fill_sign_in_form = real_fill_sign_in_form
        navigate_bill.safe_tap_recovery = real_safe_tap_recovery
        navigate_bill.SAHL_PASSWORD = real_sahl_password
        navigate_bill.time.sleep = real_sleep

    RECOVERY_CHECKS = []

    # 1. neither condition present - no-op, no side effects
    call_log = _install_recovery_mocks()
    try:
        state = {"attempted": False, "retry_count": 0}
        result = attempt_session_recovery(["Some Normal Screen"], state)
        RECOVERY_CHECKS.append((
            "neither condition present - returns False, no taps/fills",
            result is False and call_log == [],
        ))
    finally:
        _restore_recovery_mocks()

    # 2. session-expired dialog present - dismisses it AND resets
    #    sign_in_state even if it was already True (fresh expiry needs
    #    a fresh sign-in attempt)
    call_log = _install_recovery_mocks()
    try:
        state = {"attempted": True, "retry_count": 2}
        result = attempt_session_recovery(["Session Expired\nOk"], state)
        RECOVERY_CHECKS.append((
            "session-expired dialog - dismisses it, resets sign_in_state, returns True",
            result is True and call_log == ["dismiss_expired"]
            and state == {"attempted": False, "retry_count": 0},
        ))
    finally:
        _restore_recovery_mocks()

    # 3. sign-in screen, not yet attempted, password set, fill succeeds
    #    - fills the form and taps Sign in, marks attempted
    call_log = _install_recovery_mocks(fill_sign_in_form_result=True)
    try:
        state = {"attempted": False, "retry_count": 0}
        result = attempt_session_recovery([SIGN_IN_SCREEN_MARKER], state)
        RECOVERY_CHECKS.append((
            "sign-in screen, first attempt - fills form, taps Sign in, marks attempted",
            result is True and call_log == ["fill_form", "tap:Sign in"]
            and state["attempted"] is True,
        ))
    finally:
        _restore_recovery_mocks()

    # 4. sign-in screen, not yet attempted, but SAHL_PASSWORD missing -
    #    must raise, never silently proceed with no password
    call_log = _install_recovery_mocks(password=None)
    try:
        state = {"attempted": False, "retry_count": 0}
        raised = False
        try:
            attempt_session_recovery([SIGN_IN_SCREEN_MARKER], state)
        except SessionRecoveryFailed:
            raised = True
        RECOVERY_CHECKS.append((
            "sign-in screen, SAHL_PASSWORD missing - raises SessionRecoveryFailed, never fills",
            raised and call_log == [],
        ))
    finally:
        _restore_recovery_mocks()

    # 5. sign-in screen, fill fails even after the one built-in retry -
    #    must raise, not silently tap Sign in on an empty/unfilled form
    call_log = _install_recovery_mocks(fill_sign_in_form_result=False)
    try:
        state = {"attempted": False, "retry_count": 0}
        raised = False
        try:
            attempt_session_recovery([SIGN_IN_SCREEN_MARKER], state)
        except SessionRecoveryFailed:
            raised = True
        RECOVERY_CHECKS.append((
            "sign-in screen, fill fails twice - raises SessionRecoveryFailed, never taps Sign in",
            raised and "tap:Sign in" not in call_log,
        ))
    finally:
        _restore_recovery_mocks()

    # 6. sign-in screen, already attempted, still under retry budget -
    #    waits and retries, does NOT re-fill the form
    call_log = _install_recovery_mocks()
    try:
        state = {"attempted": True, "retry_count": 0}
        result = attempt_session_recovery([SIGN_IN_SCREEN_MARKER], state)
        RECOVERY_CHECKS.append((
            "sign-in screen, already attempted, under retry budget - waits, does NOT re-fill",
            result is True and call_log == [] and state["retry_count"] == 1,
        ))
    finally:
        _restore_recovery_mocks()

    # 7. sign-in screen, already attempted, retry budget exhausted -
    #    must raise, not loop forever
    call_log = _install_recovery_mocks()
    try:
        state = {"attempted": True, "retry_count": MAX_SIGN_IN_RETRIES}
        raised = False
        try:
            attempt_session_recovery([SIGN_IN_SCREEN_MARKER], state)
        except SessionRecoveryFailed:
            raised = True
        RECOVERY_CHECKS.append((
            "sign-in screen, retry budget exhausted - raises SessionRecoveryFailed",
            raised,
        ))
    finally:
        _restore_recovery_mocks()

    for description, passed in RECOVERY_CHECKS:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  attempt_session_recovery: {description}")

    # attempt_otp_recovery: same mocked-call-log style as
    # attempt_session_recovery above - never touches adb or Slack for
    # real. Covers: marker absent (no-op), Slack not configured (raises
    # before any dump/type/tap), no code within the poll timeout
    # (raises), and the full happy path (types the code, taps Verify).
    real_post_slack_alert = navigate_bill.post_slack_alert
    real_poll_for_otp_code = navigate_bill.poll_for_otp_code
    real_dump_screen_until_labeled = navigate_bill.dump_screen_until_labeled
    real_clear_and_type_field = navigate_bill.clear_and_type_field
    real_dismiss_keyboard = navigate_bill.dismiss_keyboard
    real_safe_tap_otp = navigate_bill.safe_tap

    OTP_FIELD_ELEMENTS = [
        {"label": OTP_SCREEN_MARKER, "clickable": False, "class": "android.view.View",
         "nearby_label": None, "bounds": "[312,208][768,274]", "is_password": False},
        {"label": "", "clickable": True, "class": "android.widget.EditText",
         "nearby_label": "Enter the 6 Digit Code", "bounds": "[173,722][908,827]", "is_password": False},
    ]

    # What the screen shows once Sahl has left the OTP screen.
    HOME_ELEMENTS = [
        {"label": "Internet", "clickable": True, "class": "android.view.View",
         "nearby_label": None, "bounds": "[60,1400][520,1700]", "is_password": False},
    ]

    def _install_otp_mocks(alert_ts="1234.5678", code="123456", fill_result=True, screens=None):
        """screens: what successive dumps return, in order; the last one
        repeats once the list runs out. Defaults to the OTP screen forever."""
        call_log = []
        screens = list(screens or [OTP_FIELD_ELEMENTS])

        def _dump():
            call_log.append("dump")
            return "fake_xml_path", (screens.pop(0) if len(screens) > 1 else screens[0])

        navigate_bill.post_slack_alert = lambda text: call_log.append(f"alert:{text!r}") or alert_ts
        navigate_bill.poll_for_otp_code = lambda ts: call_log.append(f"poll:{ts!r}") or code
        navigate_bill.dump_screen_until_labeled = _dump
        navigate_bill.clear_and_type_field = lambda text, xml_path=None: (
            call_log.append(f"type:{text!r}") or fill_result
        )
        navigate_bill.dismiss_keyboard = lambda: call_log.append("dismiss_keyboard")
        navigate_bill.safe_tap = lambda label: call_log.append(f"tap:{label}")
        navigate_bill.time.sleep = lambda seconds: None
        return call_log

    def _restore_otp_mocks():
        navigate_bill.post_slack_alert = real_post_slack_alert
        navigate_bill.poll_for_otp_code = real_poll_for_otp_code
        navigate_bill.dump_screen_until_labeled = real_dump_screen_until_labeled
        navigate_bill.clear_and_type_field = real_clear_and_type_field
        navigate_bill.dismiss_keyboard = real_dismiss_keyboard
        navigate_bill.safe_tap = real_safe_tap_otp
        navigate_bill.time.sleep = real_sleep

    OTP_RECOVERY_CHECKS = []

    # 1. marker not present - no-op, no side effects at all
    call_log = _install_otp_mocks()
    try:
        result = attempt_otp_recovery(["Some Normal Screen"])
        OTP_RECOVERY_CHECKS.append((
            "OTP marker not present - returns False, no Slack/adb calls",
            result is False and call_log == [],
        ))
    finally:
        _restore_otp_mocks()

    # 2. marker present, Slack not configured - raises before any dump/type/tap
    call_log = _install_otp_mocks()
    navigate_bill.post_slack_alert = lambda text: (_ for _ in ()).throw(
        SlackNotConfigured("neither SLACK_BOT_TOKEN+SLACK_CHANNEL_ID nor SLACK_WEBHOOK_URL is set in .env")
    )
    try:
        raised = False
        try:
            attempt_otp_recovery([OTP_SCREEN_MARKER])
        except OtpRecoveryFailed:
            raised = True
        OTP_RECOVERY_CHECKS.append((
            "OTP screen, Slack not configured - raises OtpRecoveryFailed",
            raised,
        ))
    finally:
        _restore_otp_mocks()

    # 3. marker present, alert posted, but no code arrives - raises
    call_log = _install_otp_mocks(code=None)
    try:
        raised = False
        try:
            attempt_otp_recovery([OTP_SCREEN_MARKER])
        except OtpRecoveryFailed:
            raised = True
        OTP_RECOVERY_CHECKS.append((
            "OTP screen, no code from Slack within timeout - raises OtpRecoveryFailed, "
            "still posts the alert first",
            raised and call_log[0].startswith("alert:"),
        ))
    finally:
        _restore_otp_mocks()

    # 4. full happy path - alert, poll, type the code, dismiss keyboard, tap
    # Verify, then wait until the OTP screen is actually gone
    call_log = _install_otp_mocks(
        alert_ts="1234.5678", code="123456", screens=[OTP_FIELD_ELEMENTS, HOME_ELEMENTS],
    )
    try:
        result = attempt_otp_recovery([OTP_SCREEN_MARKER])
        OTP_RECOVERY_CHECKS.append((
            "OTP screen, code arrives from Slack - types it, taps Verify, waits for the screen to change",
            result is True and call_log[0].startswith("alert:") and call_log[1:] == [
                "poll:'1234.5678'", "dump", "type:'123456'", "dismiss_keyboard", "tap:Verify", "dump",
            ],
        ))
    finally:
        _restore_otp_mocks()

    # 5. the live failure: by the time a code came back from Slack, Sahl had
    # already accepted the previous Verify and left the OTP screen - must
    # carry on, not raise "expected exactly one OTP EditText field, found 0"
    call_log = _install_otp_mocks(screens=[HOME_ELEMENTS])
    try:
        result = attempt_otp_recovery([OTP_SCREEN_MARKER])
        OTP_RECOVERY_CHECKS.append((
            "OTP screen gone before typing (late Verify accepted) - returns True, types and taps nothing",
            result is True and not any(
                entry.startswith(("type:", "tap:")) for entry in call_log
            ),
        ))
    finally:
        _restore_otp_mocks()

    # 6. still on the OTP screen after Verify (code rejected) - waits the
    # full settle window once, never types a second code itself
    call_log = _install_otp_mocks(screens=[OTP_FIELD_ELEMENTS])
    try:
        result = attempt_otp_recovery([OTP_SCREEN_MARKER])
        OTP_RECOVERY_CHECKS.append((
            "OTP screen still showing after Verify - waits the settle window, types only once",
            result is True
            and call_log.count("dump") == 1 + navigate_bill.OTP_VERIFY_SETTLE_CHECKS
            and sum(entry.startswith("type:") for entry in call_log) == 1,
        ))
    finally:
        _restore_otp_mocks()

    for description, passed in OTP_RECOVERY_CHECKS:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  attempt_otp_recovery: {description}")

    # slack_otp.post_slack_alert / poll_for_otp_code: mocked HTTP calls,
    # zero real network access. Covers both credential shapes documented
    # in slack_otp.py's module docstring.
    class _FakeResponse:
        def __init__(self, json_payload):
            self._json_payload = json_payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._json_payload

    real_requests_post = slack_otp.requests.post
    real_requests_get = slack_otp.requests.get
    real_bot_token = slack_otp.SLACK_BOT_TOKEN
    real_channel_id = slack_otp.SLACK_CHANNEL_ID
    real_webhook_url = slack_otp.SLACK_WEBHOOK_URL
    real_slack_sleep = slack_otp.time.sleep

    SLACK_OTP_CHECKS = []

    # bot token configured - post_slack_alert uses chat.postMessage and
    # returns the message's ts
    slack_otp.SLACK_BOT_TOKEN = "xoxb-fake"
    slack_otp.SLACK_CHANNEL_ID = "C123"
    slack_otp.SLACK_WEBHOOK_URL = None
    slack_otp.requests.post = lambda url, **kwargs: _FakeResponse({"ok": True, "ts": "1111.2222"})
    try:
        ts = slack_otp.post_slack_alert("test")
        SLACK_OTP_CHECKS.append((
            "post_slack_alert with bot token - posts via chat.postMessage, returns ts",
            ts == "1111.2222",
        ))
    finally:
        slack_otp.requests.post = real_requests_post

    # webhook only - post_slack_alert posts to the webhook URL, returns None
    slack_otp.SLACK_BOT_TOKEN = None
    slack_otp.SLACK_CHANNEL_ID = None
    slack_otp.SLACK_WEBHOOK_URL = "https://hooks.slack.example/fake"
    slack_otp.requests.post = lambda url, **kwargs: _FakeResponse({})
    try:
        ts = slack_otp.post_slack_alert("test")
        SLACK_OTP_CHECKS.append((
            "post_slack_alert with webhook only - posts, returns None (no ts to read back with)",
            ts is None,
        ))
    finally:
        slack_otp.requests.post = real_requests_post

    # neither configured - raises SlackNotConfigured
    slack_otp.SLACK_BOT_TOKEN = None
    slack_otp.SLACK_CHANNEL_ID = None
    slack_otp.SLACK_WEBHOOK_URL = None
    try:
        raised = False
        try:
            slack_otp.post_slack_alert("test")
        except SlackNotConfigured:
            raised = True
        SLACK_OTP_CHECKS.append((
            "post_slack_alert with nothing configured - raises SlackNotConfigured",
            raised,
        ))
    finally:
        pass

    # poll_for_otp_code with bot token - finds a 6-digit code in history
    slack_otp.SLACK_BOT_TOKEN = "xoxb-fake"
    slack_otp.SLACK_CHANNEL_ID = "C123"
    slack_otp.requests.get = lambda url, **kwargs: _FakeResponse(
        {"ok": True, "messages": [{"ts": "2222.3333", "text": "OTP: 654321"}]}
    )
    slack_otp.time.sleep = lambda seconds: None
    try:
        code = slack_otp.poll_for_otp_code("1111.2222", timeout_seconds=5.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code with bot token - finds the 6-digit code in a reply",
            code == "654321",
        ))
    finally:
        slack_otp.requests.get = real_requests_get

    # THE live bug: the phone's Shortcut fires the instant the SMS
    # lands, so the code is routinely posted BEFORE the script has
    # detected the OTP screen and alerted. A code older than after_ts
    # but inside OTP_LOOKBACK_SECONDS must still be found - under the
    # original "strictly after the alert" filter this returned None.
    alert_ts = "2000.0000"
    code_before_alert_ts = str(float(alert_ts) - 60.0)  # 60s before the alert
    slack_otp.requests.get = lambda url, **kwargs: _FakeResponse(
        {"ok": True, "messages": [{"ts": code_before_alert_ts, "text": "384496"}]}
    )
    try:
        code = slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=5.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code - code posted BEFORE the alert but within the "
            "lookback window is still found (the live race)",
            code == "384496",
        ))
    finally:
        slack_otp.requests.get = real_requests_get

    # The self-match trap the lookback window exposes: the alert itself
    # is now INSIDE the window, and a full isoformat() timestamp ends
    # in exactly six digits of microseconds. It must never be read back
    # as the code - it's skipped by ts.
    slack_otp.requests.get = lambda url, **kwargs: _FakeResponse(
        {"ok": True, "messages": [
            {"ts": alert_ts,
             "text": "Sahl bill-pay run needs an OTP code to continue "
                     "(detected at 2026-09-20T14:28:13.796712)."},
        ]}
    )
    try:
        code = slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=2.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code - never reads its OWN alert's microseconds "
            "(796712) back as the OTP code",
            code is None,
        ))
    finally:
        slack_otp.requests.get = real_requests_get

    # More than one code in the backwards window (e.g. a stale one from
    # a previous attempt): the NEWEST wins, regardless of the order
    # Slack returns them in - so this deliberately lists the stale one
    # first rather than relying on Slack's own ordering.
    slack_otp.requests.get = lambda url, **kwargs: _FakeResponse(
        {"ok": True, "messages": [
            {"ts": str(float(alert_ts) - 90.0), "text": "111111"},   # stale
            {"ts": str(float(alert_ts) - 10.0), "text": "222222"},   # newest
            {"ts": str(float(alert_ts) - 50.0), "text": "333333"},
        ]}
    )
    try:
        code = slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=5.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code - with several codes in the window, the NEWEST wins",
            code == "222222",
        ))
    finally:
        slack_otp.requests.get = real_requests_get

    # The live bug: a second OTP check in the same run must not get the
    # first, already-used code back from inside the lookback window.
    used_ts = str(float(alert_ts) - 5.0)
    slack_otp.requests.get = lambda url, **kwargs: _FakeResponse(
        {"ok": True, "messages": [{"ts": used_ts, "text": "433883"}]}
    )
    try:
        first = slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=2.0, poll_interval=1.0)
        second = slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=2.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code - never hands out the same Slack message's code twice",
            first == "433883" and second is None,
        ))
    finally:
        slack_otp.requests.get = real_requests_get
        slack_otp._USED_CODE_TS.discard(used_ts)

    # The window itself: the outgoing `oldest` param must actually be
    # rolled back by OTP_LOOKBACK_SECONDS, so the window can't silently
    # regress to "after the alert only" without a test noticing.
    captured_params = {}

    def _capture_get(url, **kwargs):
        captured_params.update(kwargs.get("params", {}))
        return _FakeResponse({"ok": True, "messages": []})

    slack_otp.requests.get = _capture_get
    try:
        slack_otp.poll_for_otp_code(alert_ts, timeout_seconds=1.0, poll_interval=1.0)
        expected_oldest = float(alert_ts) - slack_otp.OTP_LOOKBACK_SECONDS
        SLACK_OTP_CHECKS.append((
            f"poll_for_otp_code - queries Slack from alert_ts minus "
            f"OTP_LOOKBACK_SECONDS ({slack_otp.OTP_LOOKBACK_SECONDS}s), not from alert_ts",
            float(captured_params.get("oldest", 0)) == expected_oldest,
        ))
    finally:
        slack_otp.requests.get = real_requests_get

    # poll_for_otp_code with webhook only - can't read history, times out to None
    slack_otp.SLACK_BOT_TOKEN = None
    slack_otp.SLACK_CHANNEL_ID = None
    try:
        code = slack_otp.poll_for_otp_code("1111.2222", timeout_seconds=5.0, poll_interval=1.0)
        SLACK_OTP_CHECKS.append((
            "poll_for_otp_code with webhook only - can't read replies, returns None",
            code is None,
        ))
    finally:
        pass

    slack_otp.SLACK_BOT_TOKEN = real_bot_token
    slack_otp.SLACK_CHANNEL_ID = real_channel_id
    slack_otp.SLACK_WEBHOOK_URL = real_webhook_url
    slack_otp.requests.post = real_requests_post
    slack_otp.requests.get = real_requests_get
    slack_otp.time.sleep = real_slack_sleep

    for description, passed in SLACK_OTP_CHECKS:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  slack_otp: {description}")

    # ask_ai_free_choice: verification logic (does it actually refuse a
    # target that isn't really tappable/editable, a value_key that
    # isn't really known?) - mocked Gemini client, zero real API calls.
    class _FakeGenAIResponse:
        def __init__(self, text):
            self.text = text

    class _FakeModels:
        def __init__(self, response_text):
            self._response_text = response_text

        def generate_content(self, model, contents, config):
            return _FakeGenAIResponse(self._response_text)

    class _FakeGenAIClient:
        def __init__(self, response_text):
            self.models = _FakeModels(response_text)

    FREE_CHOICE_ELEMENTS = [
        {"label": "Landline", "clickable": True, "class": "android.view.View",
         "nearby_label": None, "bounds": "[0,0][10,10]", "is_password": False},
        {"label": None, "clickable": True, "class": "android.widget.EditText",
         "nearby_label": "Account Number", "bounds": "[0,10][10,20]", "is_password": False},
    ]
    FREE_CHOICE_ALL_LABELS = ["Landline", "Account Number"]
    FREE_CHOICE_KNOWN_VALUES = {"operator": "WE"}

    FREE_CHOICE_AI_CASES = [
        (
            "valid tap on an actually-tappable label - passes through",
            '{"action_type": "tap", "target_label": "Landline", "target_nearby_label": null, "value_key": null, "reasoning": "go"}',
            {"action_type": "tap", "target_label": "Landline"},
        ),
        (
            "tap target that ISN'T actually tappable - refused to 'none'",
            '{"action_type": "tap", "target_label": "Nonexistent Button", "target_nearby_label": null, "value_key": null, "reasoning": "go"}',
            {"action_type": "none", "target_label": None},
        ),
        (
            "valid type with a real known_values key and a real field - passes through",
            '{"action_type": "type", "target_label": null, "target_nearby_label": "Account Number", "value_key": "operator", "reasoning": "go"}',
            {"action_type": "type", "target_nearby_label": "Account Number", "value_key": "operator"},
        ),
        (
            "type with a value_key that ISN'T in known_values - refused to 'none'",
            '{"action_type": "type", "target_label": null, "target_nearby_label": "Account Number", "value_key": "made_up_key", "reasoning": "go"}',
            {"action_type": "none", "value_key": None},
        ),
        (
            "type targeting a field that doesn't exist - refused to 'none'",
            '{"action_type": "type", "target_label": null, "target_nearby_label": "Nonexistent Field", "value_key": "operator", "reasoning": "go"}',
            {"action_type": "none", "target_nearby_label": None},
        ),
        (
            "invalid JSON from the AI - refused to 'none', never crashes",
            "not valid json at all",
            {"action_type": "none"},
        ),
        (
            "AI invents an action_type outside the enum - refused to 'none'",
            '{"action_type": "delete_everything", "target_label": "Landline", "target_nearby_label": null, "value_key": null, "reasoning": "go"}',
            {"action_type": "none"},
        ),
    ]

    real_ai_pick_client = ai_pick.client
    for description, fake_response_text, expected_subset in FREE_CHOICE_AI_CASES:
        ai_pick.client = _FakeGenAIClient(fake_response_text)
        try:
            result = ask_ai_free_choice(
                "test goal", FREE_CHOICE_ALL_LABELS, FREE_CHOICE_ELEMENTS, FREE_CHOICE_KNOWN_VALUES
            )
            passed = all(result.get(k) == v for k, v in expected_subset.items())
        finally:
            ai_pick.client = real_ai_pick_client

        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  ask_ai_free_choice: {description}")
        print(f"      expected(subset)={expected_subset!r} actual={result!r}")

    # ai_free_choice_tap / ai_free_choice_type: existence-reverification
    # against a fresh (mocked) dump - must refuse a target that isn't
    # really there, never trust the AI's claim alone. No real adb calls.
    real_dump_current_screen_tap = tap.dump_current_screen
    real_list_interesting_elements_tap = tap.list_interesting_elements
    real_find_editable_fields_tap = tap.find_editable_fields
    real_run_adb_tap = tap.run_adb

    def _install_tap_mocks(elements):
        adb_calls = []
        tap.dump_current_screen = lambda: "fake_xml_path"
        tap.list_interesting_elements = lambda xml_path: elements
        tap.find_editable_fields = lambda xml_path: [e for e in elements if e["class"] == "android.widget.EditText"]
        tap.run_adb = lambda *args, **kwargs: adb_calls.append(args)
        return adb_calls

    def _restore_tap_mocks():
        tap.dump_current_screen = real_dump_current_screen_tap
        tap.list_interesting_elements = real_list_interesting_elements_tap
        tap.find_editable_fields = real_find_editable_fields_tap
        tap.run_adb = real_run_adb_tap

    FREE_CHOICE_TAP_CASES = []

    # exists and clickable - taps it
    adb_calls = _install_tap_mocks(FREE_CHOICE_ELEMENTS)
    try:
        result = ai_free_choice_tap("Landline", reasoning="test")
        FREE_CHOICE_TAP_CASES.append((
            "target exists and is clickable - taps it",
            result is True and len(adb_calls) == 1,
        ))
    finally:
        _restore_tap_mocks()

    # doesn't exist on a fresh dump - refuses, never taps
    adb_calls = _install_tap_mocks(FREE_CHOICE_ELEMENTS)
    try:
        result = ai_free_choice_tap("Something Hallucinated", reasoning="test")
        FREE_CHOICE_TAP_CASES.append((
            "target does NOT exist on a fresh dump - refuses, zero adb calls",
            result is False and adb_calls == [],
        ))
    finally:
        _restore_tap_mocks()

    # exactly one matching editable field - types into it
    adb_calls = _install_tap_mocks(FREE_CHOICE_ELEMENTS)
    try:
        result = ai_free_choice_type("Account Number", "operator", "WE", reasoning="test")
        FREE_CHOICE_TAP_CASES.append((
            "exactly one matching field - types into it",
            result is True and len(adb_calls) == 2,  # tap to focus, then type
        ))
    finally:
        _restore_tap_mocks()

    # zero matching fields - refuses, never types
    adb_calls = _install_tap_mocks(FREE_CHOICE_ELEMENTS)
    try:
        result = ai_free_choice_type("No Such Field", "operator", "WE", reasoning="test")
        FREE_CHOICE_TAP_CASES.append((
            "zero matching fields - refuses, zero adb calls",
            result is False and adb_calls == [],
        ))
    finally:
        _restore_tap_mocks()

    # two fields with the SAME nearby_label - ambiguous, refuses rather than guessing
    ambiguous_elements = FREE_CHOICE_ELEMENTS + [
        {"label": None, "clickable": True, "class": "android.widget.EditText",
         "nearby_label": "Account Number", "bounds": "[0,20][10,30]", "is_password": False},
    ]
    adb_calls = _install_tap_mocks(ambiguous_elements)
    try:
        result = ai_free_choice_type("Account Number", "operator", "WE", reasoning="test")
        FREE_CHOICE_TAP_CASES.append((
            "two fields share the same nearby_label - refuses rather than guessing",
            result is False and adb_calls == [],
        ))
    finally:
        _restore_tap_mocks()

    for description, passed in FREE_CHOICE_TAP_CASES:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  ai_free_choice_tap/type: {description}")

    # known_values guard in navigate_pay_bill: SAHL_PASSWORD's value must
    # never be accepted into known_values, checked BEFORE any adb call.
    real_navigate_bill_password = navigate_bill.SAHL_PASSWORD
    navigate_bill.SAHL_PASSWORD = "totally-fake-test-password"
    try:
        raised = False
        try:
            navigate_pay_bill(
                service_tile_label="Landline", operator="WE", account_number="0225305397",
                known_values={"leaked": "totally-fake-test-password"},
            )
        except AssertionError:
            raised = True
        status = "PASS" if raised else "FAIL"
        if not raised:
            failures += 1
        print(f"{status}  navigate_pay_bill: known_values containing SAHL_PASSWORD's "
              f"value raises AssertionError before any adb call")
    finally:
        navigate_bill.SAHL_PASSWORD = real_navigate_bill_password

    # home_tile_tapped regression: the home-screen tile tap must fire
    # even when session recovery consumes step 1 entirely, pushing the
    # actual home screen to step 2 or later - confirmed live: a real
    # run's sign-in recovery ate step 1, the "Internet" tile only
    # appeared at step 2, and the OLD "step == 1" gate silently never
    # matched it, falling through to the tier-2 AI fallback for
    # something that should have been fully deterministic.
    real_ensure_sahl_open = navigate_bill.ensure_sahl_open
    real_dump_screen_until_labeled_nb = navigate_bill.dump_screen_until_labeled
    real_attempt_session_recovery_nb = navigate_bill.attempt_session_recovery
    real_attempt_otp_recovery_nb = navigate_bill.attempt_otp_recovery
    real_safe_tap_nb = navigate_bill.safe_tap
    real_classify_nb = navigate_bill.classify_screen_and_pick_action
    real_navigate_bill_sleep = navigate_bill.time.sleep

    home_tile_call_log = []
    # step 1's dump: the sign-in screen (recovery consumes this step
    # entirely). step 2's dump: the home screen with the tile - only
    # appears AFTER the sign-in step, same ordering as the real bug.
    fake_screens = [
        ("xml1", [{"label": SIGN_IN_SCREEN_MARKER, "clickable": False,
                   "class": "android.view.View", "nearby_label": None,
                   "bounds": "[0,0][10,10]", "is_password": False}]),
        ("xml2", [{"label": "Internet", "clickable": True,
                   "class": "android.view.View", "nearby_label": None,
                   "bounds": "[0,0][10,10]", "is_password": False}]),
    ]
    fake_screen_index = {"i": 0}

    def _fake_dump_screen_until_labeled():
        idx = min(fake_screen_index["i"], len(fake_screens) - 1)
        fake_screen_index["i"] += 1
        return fake_screens[idx]

    navigate_bill.ensure_sahl_open = lambda: True
    navigate_bill.dump_screen_until_labeled = _fake_dump_screen_until_labeled
    navigate_bill.attempt_session_recovery = lambda all_labels, sign_in_state: (
        home_tile_call_log.append("recovery_check") or SIGN_IN_SCREEN_MARKER in all_labels
    )
    navigate_bill.attempt_otp_recovery = lambda all_labels: False
    navigate_bill.safe_tap = lambda label, xml_path=None: home_tile_call_log.append(f"tap:{label}")
    navigate_bill.classify_screen_and_pick_action = lambda *a, **k: (
        home_tile_call_log.append("AI_FALLBACK_TIER2")
        or {"screen_type": "unknown", "action": None}
    )
    navigate_bill.time.sleep = lambda seconds: None

    try:
        navigate_pay_bill(
            service_tile_label="Internet", operator="WE", account_number="0225305397",
            max_steps=2,
        )
        passed = "tap:Internet" in home_tile_call_log and "AI_FALLBACK_TIER2" not in home_tile_call_log
        status = "PASS" if passed else "FAIL"
        if not passed:
            failures += 1
        print(f"{status}  navigate_pay_bill: home-tile tap still fires when session "
              f"recovery consumes step 1 (doesn't fall through to the AI fallback)")
        print(f"      call log: {home_tile_call_log!r}")
    finally:
        navigate_bill.ensure_sahl_open = real_ensure_sahl_open
        navigate_bill.dump_screen_until_labeled = real_dump_screen_until_labeled_nb
        navigate_bill.attempt_session_recovery = real_attempt_session_recovery_nb
        navigate_bill.attempt_otp_recovery = real_attempt_otp_recovery_nb
        navigate_bill.safe_tap = real_safe_tap_nb
        navigate_bill.classify_screen_and_pick_action = real_classify_nb
        navigate_bill.time.sleep = real_navigate_bill_sleep

    for description, label, expected_allowed in SESSION_EXPIRED_CASES:
        allowed, reason = check_session_expired_tap_allowed(label)
        status = "PASS" if allowed == expected_allowed else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"{status}  {description}")
        print(f"      expected_allowed={expected_allowed!r} actual_allowed={allowed!r} ({reason})")

    total = (len(CASES) + len(AMBIGUOUS_CASES) + len(SESSION_EXPIRED_CASES)
              + len(checks) + len(ADB_FAILURE_CASES) + len(STABILITY_CASES)
              + len(FEE_SUMMARY_CASES) + len(RECOVERY_CHECKS)
              + len(OTP_RECOVERY_CHECKS) + len(SLACK_OTP_CHECKS)
              + len(FREE_CHOICE_AI_CASES) + len(FREE_CHOICE_TAP_CASES)
              + len(ACCOUNT_NUMBER_FIX_CASES)
              + 1  # known_values/SAHL_PASSWORD guard
              + 1  # run_adb transient-failure recovery
              + 1  # home_tile_tapped regression
              + 1)  # countdown label vs. stability detection
    print(f"\n{total - failures}/{total} passed")
    return failures == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
