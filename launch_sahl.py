"""
Preflight step, run BEFORE navigate.py's or navigate_bill.py's own step
loop starts: gets from wherever the emulator currently is on the
Android launcher (home screen, or the all-apps drawer already open) to
Sahl actually being open. Every other flow in this project assumes
Sahl is already the foreground app - this is what makes that
assumption true.
"""

import time

from dump_screen import dump_screen_until_labeled, get_foreground_package, run_adb
from guardrail import ALLOWED_LABELS
from tap import safe_tap_by_prefix, swipe_up

LAUNCHER_PACKAGE = "com.google.android.apps.nexuslauncher"
SAHL_PACKAGE = "gtpay.gtronicspay.com"
SAHL_APP_LABEL = "Sahl"
MAX_LAUNCH_ATTEMPTS = 5

assert SAHL_APP_LABEL in ALLOWED_LABELS, (
    f"launch_sahl.SAHL_APP_LABEL {SAHL_APP_LABEL!r} must be confirmed by hand and "
    f"added to guardrail.ALLOWED_LABELS first, same as every other label this "
    f"project ever taps."
)


def ensure_sahl_open(max_attempts: int = MAX_LAUNCH_ATTEMPTS) -> bool:
    """Repeatedly checks whether we're still on the Android launcher and,
    if so, works toward opening Sahl: taps "Sahl" if the app drawer is
    already showing it, otherwise swipes up to open the drawer. Stops
    and returns True as soon as the foreground package is no longer the
    launcher's - not specifically "Sahl", since the package is the one
    thing we can read off ANY dump, launcher or otherwise, with
    certainty (see get_foreground_package). Returns False if the
    launcher is still in the foreground after max_attempts - something
    unexpected is on screen, not something to guess through."""

    for attempt in range(1, max_attempts + 1):
        xml_path, elements = dump_screen_until_labeled()

        package = get_foreground_package(xml_path)
        if package != LAUNCHER_PACKAGE:
            print(f"Left the launcher (foreground package: {package!r}) - Sahl is opening.")
            return True

        labels = {e["label"] for e in elements if e["label"]}

        # Prefix match, not exact - a pending notification changes Sahl's
        # own content-desc to "Sahl has N notification(s)", which an
        # exact "Sahl" in labels would never match. See guardrail.py's
        # ALLOWED_LABEL_PREFIXES comment for how this was found.
        if any(label.startswith(SAHL_APP_LABEL) for label in labels):
            print(f"App drawer is open - tapping the {SAHL_APP_LABEL!r} icon.")
            safe_tap_by_prefix(SAHL_APP_LABEL, xml_path)
            time.sleep(1.5)
            continue

        print(f"Still on the launcher home screen (attempt {attempt}/{max_attempts}) - swiping up.")
        swipe_up()
        time.sleep(1.0)

    print(f"Still on the launcher after {max_attempts} attempts - stopping rather than guessing.")
    return False


def reset_to_launcher() -> None:
    """Force-stops Sahl and returns to the Android home screen, so the
    next flow starts from Sahl's own home screen instead of wherever the
    previous flow left off (e.g. a fee screen) - ensure_sahl_open() only
    knows how to start from the launcher. Force-stop ends the process
    but keeps the app's stored data, so the login survives it; a sign-in
    screen on relaunch is the same case a fresh emulator boot already
    hits, and each flow handles it the same way."""

    run_adb("shell", "am", "force-stop", SAHL_PACKAGE)
    run_adb("shell", "input", "keyevent", "3")  # KEYCODE_HOME
    time.sleep(2.0)


if __name__ == "__main__":
    opened = ensure_sahl_open()
    print(f"\nensure_sahl_open() -> {opened}")
