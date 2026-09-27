"""
Step 2: decide whether it's safe to tap something, BEFORE we write any
code that's actually capable of tapping.

Two independent checks, both have to pass:

1. ALLOWLIST  - the label has to be one we've personally confirmed, by
   walking the app ourselves, is safe (a menu item, a "next" button,
   an operator icon, etc). Anything not on this list gets refused,
   even if it looks harmless.

2. DENY PATTERN - even if something slipped onto the allowlist by
   mistake, this is a second, independent check: does the label's
   text itself look like a real payment button? This is the one
   that would catch "Pay 173.93 EGP" no matter what.

A tap is only allowed if it passes BOTH. This mirrors why real banking
automation tools use two separate checks instead of one: a single
allowlist can have a mistake in it, but it's very unlikely the SAME
mistake would also slip past a completely different kind of check.
"""

import re

# Labels we've manually confirmed, screen by screen, are safe to tap.
# Add to this list only after YOU have tapped it yourself and confirmed
# it does not move money or go past the fee.
ALLOWED_LABELS = {
    "Mobile",
    "Top-Up",
    "Confirm",
    "Vodafone",
    "e&",
    "Orange",
    "WE",
    # added while building the Internet "Pay Bill" flow - each walked
    # by hand on the emulator first, same as everything above:
    "Internet",       # home-screen service tile
    "Pay Bill",       # radio option - which flow within the service
    "Proceed",        # advances the service-type screen and the
                       # network-operator dropdown
    "Enquire",        # submits the bill form to fetch the amount due
    "Etisalat",       # internet ISP option in the operator dropdown
    "Nour",           # internet ISP option in the operator dropdown
    # added while building the Landline "Pay Bill" flow - confirmed by
    # hand to follow the exact same shape as Internet's (tile -> Select
    # service type -> Pay Bill -> Proceed -> company picker -> Enquire),
    # with "WE" as the operator:
    "Landline",       # home-screen service tile
    # added for the Android-launcher preflight (getting from the OS
    # home screen into Sahl itself, before any flow above can start) -
    # confirmed by hand: tapping this in the app drawer just opens
    # Sahl, nothing else.
    "Sahl",
    # added for the mobile-verification (OTP) screen - confirmed from a
    # real user-supplied XML dump: a distinct clickable node,
    # content-desc="Verify", cleanly separate from the title/message
    # text (unlike the merged Session Expired dialog above) - tapping
    # it only ever submits the entered code, never moves money.
    "Verify",
}

# Some elements' labels change with app state - e.g. the network
# operator field reads "Network Operator\nWE" once WE is selected, so
# it can never be put on ALLOWED_LABELS as one fixed string. Instead we
# allow tapping anything whose label STARTS WITH one of these prefixes
# - still run through DENY_PATTERN just like everything else, so this
# doesn't weaken the "no payment button" guarantee, it only widens
# which known, non-payment field can be matched by name.
ALLOWED_LABEL_PREFIXES = {
    "Network Operator",
    # Sahl's app-drawer icon reads exactly "Sahl" with content-desc="Sahl"
    # normally, but Android appends to it whenever the app has a pending
    # notification - confirmed by hand from a real dump reading
    # content-desc="Sahl has 1 notification" instead. Exact-matching
    # "Sahl" alone breaks the moment a notification badge appears; this
    # prefix still goes through DENY_PATTERN like everything else, it
    # only widens which exact string of the same known icon counts.
    "Sahl",
}

# Groups of labels that are each INDIVIDUALLY already on ALLOWED_LABELS
# (hand-verified on their own, same as everything above) but that play
# the exact same structural role in different flows - e.g. Internet's
# Pay Bill form submits via "Enquire", Landline's submits via "Proceed"
# again instead (confirmed by hand). This dict never approves anything
# new by itself - it only says "these already-approved labels are
# interchangeable ways to do the one thing", so tap.safe_tap_first_present
# can pick whichever one is actually visible instead of the code having
# to hardcode which exact label a given flow happens to use.
ACTION_SYNONYMS = {
    "submit_bill_form": {"Enquire", "Proceed"},
}

# Fail loudly at import time, not deep inside a live run, if a synonym
# group ever references a label that hasn't gone through the same
# hand-verification as everything else on ALLOWED_LABELS.
for _group_name, _group_labels in ACTION_SYNONYMS.items():
    _unapproved = _group_labels - ALLOWED_LABELS
    assert not _unapproved, (
        f"guardrail.ACTION_SYNONYMS[{_group_name!r}] references label(s) not on "
        f"ALLOWED_LABELS: {_unapproved} - confirm by hand and add to ALLOWED_LABELS "
        f"first, same as every other label in this file."
    )
del _group_name, _group_labels, _unapproved

# "Pay Bill" is a category radio button - which KIND of bill flow to
# start (Pay Bill / Top-Up / Extra Packages) - not a checkout action,
# even though the word "pay" trips DENY_PATTERN below. Confirmed by
# hand: selecting it only ever leads to an account/operator form and
# then an Enquire step: it never itself moves money (the real payment
# buttons, "Pay X EGP" and "Pay All", still get blocked as normal).
# This is a single, explicitly named exception - see
# check_bill_category_tap_allowed() - never a general loosening of
# DENY_PATTERN, and never exposed to the AI'''s free choice.
BILL_CATEGORY_DENY_OVERRIDE = {
    "Pay Bill",
}

# Anything whose label matches this, we refuse no matter what -
# even if it were somehow on the allowlist above.
DENY_PATTERN = re.compile(
    r"\bpay\b|confirm.*pay|pay.*now|submit.*payment",
    re.IGNORECASE,
)


# A SEPARATE, narrower allowlist just for session-recovery actions
# (dismissing "Session Expired", re-tapping "Sign in" when the app logs
# us out). These are deliberately kept OUT of ALLOWED_LABELS above:
# "Ok" in particular is dangerously generic for a payment app - if it
# were in the same pool the AI's normal step-loop picks from, it could
# end up being tapped out of context on some other dialog we haven't
# seen yet. Recovery taps are only ever triggered by hardcoded, tightly
# scoped conditions in navigate.py (e.g. "Ok" only fires when "Session
# Expired" is ALSO present on screen) - never by the AI's free choice.
RECOVERY_ALLOWED_LABELS = {
    "Ok",
    "Sign in",
}

# The bill flow's "Session Expired" dialog renders differently from the
# one navigate.py's Mobile Top-Up flow sees: instead of separate
# elements for the title and the "Ok" button, the WHOLE dialog is one
# clickable element whose content-desc merges the title, the message,
# AND the button text into a single string - confirmed by hand from a
# real XML dump: "Session Expired\n108 : Your session has expired.
# Please sign in again to continue\nOk". There's no element whose label
# is exactly "Ok" to check against RECOVERY_ALLOWED_LABELS above, so
# this needs its own narrow, substring-based exception instead - same
# spirit as BILL_CATEGORY_DENY_OVERRIDE. Only ever invoked from a
# hardcoded, tightly-scoped recovery condition in navigate_bill.py -
# never exposed to the AI's free choice, same as every other recovery
# check in this file.
SESSION_EXPIRED_MARKER_TEXT = "Session Expired"

# Marks Sahl's mobile-verification (OTP) screen - a real user-supplied
# XML dump shows a stable title node, content-desc="Mobile Verification",
# unrelated to and unlike SESSION_EXPIRED_MARKER_TEXT's merged-node
# situation: the EditText and the "Verify" button are each their own
# separate, cleanly clickable nodes here, so no special-cased tap logic
# is needed for this screen the way it is for Session Expired - only
# detecting when it's showing at all.
OTP_SCREEN_MARKER = "Mobile Verification"


def check_session_expired_tap_allowed(label: str) -> tuple[bool, str]:
    """Special-cased recovery check for the bill flow's merged Session
    Expired/Ok dialog - see SESSION_EXPIRED_MARKER_TEXT above. Still
    runs the same deny-pattern check as every other tap - defense in
    depth even for a single hardcoded exception."""

    if label is None:
        return False, "no label at all - refusing, nothing to check against"

    if DENY_PATTERN.search(label):
        return False, f"REFUSED: label {label!r} looks like a payment button"

    if SESSION_EXPIRED_MARKER_TEXT not in label:
        return False, f"REFUSED: label {label!r} does not contain {SESSION_EXPIRED_MARKER_TEXT!r}"

    return True, (
        f"allowed (session-expired dialog exception): {label!r} confirmed by hand "
        f"to be the merged Session Expired/Ok dialog"
    )


def check_recovery_tap_allowed(label: str) -> tuple[bool, str]:
    """Same idea as check_tap_allowed, but checks against the separate,
    narrower RECOVERY_ALLOWED_LABELS instead. Still runs the same deny
    pattern check - defense in depth even for a scoped-down list."""

    if label is None:
        return False, "no label at all - refusing, nothing to check against"

    if DENY_PATTERN.search(label):
        return False, f"REFUSED: label {label!r} looks like a payment button"

    if label not in RECOVERY_ALLOWED_LABELS:
        return False, f"REFUSED: label {label!r} is not on the recovery allowlist"

    return True, f"allowed (recovery): {label!r} is on the recovery allowlist and not a payment button"


def check_tap_allowed(label: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Always check `allowed` before tapping."""

    if label is None:
        return False, "no label at all - refusing, nothing to check against"

    if DENY_PATTERN.search(label):
        return False, f"REFUSED: label {label!r} looks like a payment button"

    on_allowlist = label in ALLOWED_LABELS
    matches_prefix = any(label.startswith(prefix) for prefix in ALLOWED_LABEL_PREFIXES)

    if not on_allowlist and not matches_prefix:
        return False, f"REFUSED: label {label!r} is not on the allowlist yet"

    return True, f"allowed: {label!r} is on the allowlist and not a payment button"


def check_bill_category_tap_allowed(label: str) -> tuple[bool, str]:
    """A single, hand-verified exception to DENY_PATTERN for "Pay Bill"
    - see BILL_CATEGORY_DENY_OVERRIDE above for why this is safe. Only
    ever called from navigate_bill.py's hardcoded tap of this exact
    string - never from the AI'''s free choice."""

    if label not in BILL_CATEGORY_DENY_OVERRIDE:
        return False, f"REFUSED: {label!r} is not the hand-verified bill-category exception"

    if label not in ALLOWED_LABELS:
        return False, f"REFUSED: {label!r} is not on the allowlist"

    return True, f"allowed (bill-category exception): {label!r} confirmed by hand to be a category selector, not a payment action"


if __name__ == "__main__":
    # quick sanity test using the real labels we found on Sahl's screen
    test_labels = ["Mobile", "Top-Up", "Pay 173.93 EGP", "Vodafone", "Something Random"]

    for label in test_labels:
        allowed, reason = check_tap_allowed(label)
        status = "ALLOWED" if allowed else "BLOCKED"
        print(f"{status:8s} {label!r:25s} -> {reason}")
