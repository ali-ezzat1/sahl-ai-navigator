"""
Step 1: read what's on the emulator's screen right now, from Python
instead of typing adb commands by hand.

This does exactly what we were doing manually:
  adb shell uiautomator dump
  adb pull /sdcard/window_dump.xml
...and then reads that XML file and prints just the useful parts:
every element that has a label (content-desc) or is tappable, plus
where it is on the screen.
"""

import subprocess
import time
import xml.etree.ElementTree as ET

DUMP_PATH_ON_PHONE = "/sdcard/window_dump.xml"
DUMP_PATH_LOCAL = "window_dump.xml"

# Confirmed live: adb can die mid-command with exit code 137 (SIGKILL)
# while the emulator itself is still open and responsive - not a real
# device/emulator problem, most likely something else on the machine
# (Android Studio's device panel, scrcpy, etc.) momentarily restarting
# the adb server out from under the in-flight command. Retrying a
# couple of times before giving up recovers from exactly this, without
# masking a genuinely dead adb/emulator (FileNotFoundError - adb not on
# PATH at all - is NOT retried below, since retrying can't fix that).
ADB_RETRY_ATTEMPTS = 3
ADB_RETRY_DELAY_SECONDS = 1.5


class AdbConnectionError(Exception):
    """Raised when an adb command still fails after ADB_RETRY_ATTEMPTS -
    non-zero exit, a timeout, or adb itself not found on PATH (that one
    is never retried - see run_adb). Every adb call in this project
    goes through run_adb() below, so this is the ONE exception type
    callers ever need to catch."""


def run_adb(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Runs `adb <args>`, raising AdbConnectionError with a clear
    message if it still fails after ADB_RETRY_ATTEMPTS attempts, instead
    of letting a raw CalledProcessError/TimeoutExpired propagate as an
    unhandled traceback. Every adb-shelling function in this project
    (here and in tap.py) calls this instead of subprocess.run directly.

    Retries CalledProcessError and TimeoutExpired - both can be
    transient (a momentary adb server restart, a slow device) - but
    NOT FileNotFoundError, which means adb itself isn't on PATH at all;
    that's a permanent environment problem no amount of retrying fixes."""

    for attempt in range(1, ADB_RETRY_ATTEMPTS + 1):
        try:
            return subprocess.run(["adb", *args], check=True, timeout=timeout)
        except FileNotFoundError as e:
            raise AdbConnectionError(
                "adb was not found - is it installed and on PATH?"
            ) from e
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            if attempt == ADB_RETRY_ATTEMPTS:
                if isinstance(e, subprocess.CalledProcessError):
                    raise AdbConnectionError(
                        f"adb command failed (exit code {e.returncode}): adb {' '.join(args)}"
                    ) from e
                raise AdbConnectionError(
                    f"adb command timed out after {timeout}s: adb {' '.join(args)}"
                ) from e
            print(f"adb command failed on attempt {attempt}/{ADB_RETRY_ATTEMPTS} "
                  f"(adb {' '.join(args)}) - retrying in {ADB_RETRY_DELAY_SECONDS}s. "
                  f"Often just a momentary adb server restart, not a real "
                  f"device/emulator problem.")
            time.sleep(ADB_RETRY_DELAY_SECONDS)


def dump_current_screen() -> str:
    """Ask the emulator to write out what's currently on screen, then
    copy that file down to this computer. Returns the local file path."""

    # same as typing: adb shell uiautomator dump
    run_adb("shell", "uiautomator", "dump", DUMP_PATH_ON_PHONE)

    # same as typing: adb pull /sdcard/window_dump.xml
    run_adb("pull", DUMP_PATH_ON_PHONE, DUMP_PATH_LOCAL)

    return DUMP_PATH_LOCAL


def list_interesting_elements(xml_path: str):
    """Read the dumped XML and yield only the elements worth knowing
    about: anything with a label, or anything you can tap.

    Also tags every element with `nearby_label`: the last non-empty
    label seen just before it, walking the XML top to bottom. That's
    the same trick a human uses to know an unlabeled box means
    "Mobile Number" - it's the field right after that heading."""

    tree = ET.parse(xml_path)

    last_label_seen = None

    for node in tree.iter("node"):
        # some widgets (radio buttons in particular) pad their
        # content-desc with a trailing space - e.g. "Pay Bill " - which
        # would otherwise silently fail to match an allowlist entry
        # that was written down as "Pay Bill". Stripping is purely
        # cosmetic cleanup, it doesn't change what the deny-pattern or
        # allowlist checks see in any meaningful way.
        raw_label = node.get("content-desc") or node.get("text")
        label = raw_label.strip() if raw_label else None
        if label == "":
            label = None
        clickable = node.get("clickable") == "true"
        element_class = node.get("class")

        if not label and not clickable:
            continue  # skip invisible layout containers, not useful to us

        yield {
            "label": label,
            "clickable": clickable,
            "bounds": node.get("bounds"),
            "class": element_class,
            "nearby_label": last_label_seen,
            # the sign-in screen has two EditText fields with identical
            # nearby_label ("+20", the country-code icon) - nothing
            # else in the tree tells them apart except this attribute,
            # which Android sets on password fields specifically to
            # mask their input - confirmed against a real XML dump.
            "is_password": node.get("password") == "true",
        }

        # an EditText's own (empty) label should NOT overwrite the
        # context - only update once we've yielded this element
        if label:
            last_label_seen = label


# Labels that change on their own between reads with no user action -
# a live countdown, for instance. dump_screen_until_labeled() below
# decides a screen has finished loading by comparing two consecutive
# reads, and a self-updating label makes that comparison NEVER match,
# so it burns its entire max_wait_seconds on a screen that was ready
# immediately. Confirmed live on Sahl's OTP screen, whose "Resend OTP
# in 35s" ticks down once a second: every visit to that screen cost a
# full 10s timeout and ~19 wasted dumps, which is also what let the
# phone's OTP relay beat the script's own Slack alert (see
# slack_otp.OTP_LOOKBACK_SECONDS).
#
# Matched by PREFIX deliberately - same idiom as guardrail.py's
# ALLOWED_LABEL_PREFIXES, and far safer than a generic "ignore any
# label containing digits" rule, which would also mask a fee screen
# whose amounts are still recalculating - exactly the case this
# stability check exists to catch.
VOLATILE_LABEL_PREFIXES = (
    "Resend OTP in",
)


def _stability_signature(elements) -> frozenset:
    """The labels used to decide whether two consecutive reads show the
    same screen: everything with a label, minus the self-updating ones
    (see VOLATILE_LABEL_PREFIXES). Only the COMPARISON ignores these -
    callers still receive every element, countdown included."""

    return frozenset(
        e["label"] for e in elements
        if e["label"] and not e["label"].startswith(VOLATILE_LABEL_PREFIXES)
    )


def dump_screen_until_labeled(
    max_wait_seconds: float = 10.0,
    poll_interval: float = 1.0,
    stability_check_seconds: float = 0.5,
):
    """Repeatedly dumps the screen until the labeled elements on it are
    STABLE across two consecutive reads, or max_wait_seconds elapses -
    whichever comes first.

    Meant to replace a single dump right after an action that triggers
    a loading transition (e.g. a network call to fetch a fee amount) -
    a fixed sleep is either too short on a slow load (catching a blank,
    still-loading screen - confirmed by hand: this exact race produced
    `Labels seen: []` right after tapping Enquire) or wastes time
    waiting out a fixed duration on a fast one.

    Requiring stability, not just non-emptiness, matters because a
    loading screen can have its OWN incidental label (a header, a
    "Loading..." toast with a content-desc) while the real content is
    still populating across frames - a plain "any label present" check
    would stop right there, on content that isn't the real screen yet.
    Two consecutive reads catches that: the transient label set won't
    match the one that replaces it.

    Labels that update on their own every second (a countdown) are
    excluded from that comparison - see VOLATILE_LABEL_PREFIXES - since
    they'd otherwise guarantee the two reads never match and force a
    full max_wait_seconds timeout on a screen that was ready the whole
    time. They're still returned to the caller in `elements`.

    While a dump has no labels at all, polls at the full poll_interval
    cadence (same as before). Once a dump DOES have labels, the
    confirmation re-check uses the shorter stability_check_seconds gap
    instead of the full poll_interval - so the common case (the screen
    was already fully loaded and stable) costs one short confirmation
    dump, not a full extra poll_interval's worth of waiting.

    Returns (xml_path, elements) from whichever dump it stopped on. If
    it timed out, elements may still have no labeled entries, or still
    be unstable - the caller handles that exactly like any other
    unrecognized/empty screen, this only avoids giving up prematurely
    while something is still loading."""

    waited = 0.0
    previous_labels = None
    xml_path = dump_current_screen()
    elements = list(list_interesting_elements(xml_path))

    while True:
        labels_now = _stability_signature(elements)

        if labels_now and labels_now == previous_labels:
            return xml_path, elements

        previous_labels = labels_now

        if waited >= max_wait_seconds:
            return xml_path, elements

        gap = stability_check_seconds if labels_now else poll_interval
        time.sleep(gap)
        waited += gap

        xml_path = dump_current_screen()
        elements = list(list_interesting_elements(xml_path))


def get_foreground_package(xml_path: str) -> str | None:
    """Returns the package name of whatever app is currently in the
    foreground, read straight off the dump. Every node in a
    uiautomator dump carries the SAME package attribute - confirmed by
    hand comparing a home-screen dump and an app-drawer dump, every
    single node in both was "com.google.android.apps.nexuslauncher" -
    so the first node's value is as good as any other's. Returns None
    if the dump has no nodes at all."""

    tree = ET.parse(xml_path)
    first_node = tree.find(".//node")
    return first_node.get("package") if first_node is not None else None


def find_editable_fields(xml_path: str):
    """Returns every EditText element on screen, each with the
    nearby_label context that hints at what it's for."""

    return [
        element
        for element in list_interesting_elements(xml_path)
        if element["class"] == "android.widget.EditText"
    ]


if __name__ == "__main__":
    xml_path = dump_current_screen()
    print(f"Dumped screen to {xml_path}\n")

    for element in list_interesting_elements(xml_path):
        tap_marker = "[TAPPABLE]" if element["clickable"] else "          "
        print(f"{tap_marker} {element['label']!r:40s} {element['bounds']}")
