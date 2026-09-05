"""
Step 6: once navigate.py has gotten us to the amount-entry screen (it
stops there on purpose - typing a specific balance amount is a
deliberate choice you make, not something the AI should guess), this
script tries several balance tiers one at a time, on that SAME
screen, and records the resulting fee breakdown for each - without
ever navigating away and without ever tapping Pay.

The LEFT field on this screen is the actual balance/airtime that
arrives on the phone. Sahl computes everything else live from that:
the RIGHT field and the Service Value / Service Fees / Total Amount
readout auto-update as soon as you type a number into the left field
(VAT/tax gets added to reach the pre-fee price, then a flat service
fee is added on top for the total you'd actually pay).

Run this AFTER navigate.py has stopped on the amount screen (it hands
off there with a message about a text field waiting for input).
"""

import time

from dump_screen import dump_current_screen, list_interesting_elements
from tap import clear_and_type_field_at_position, dismiss_keyboard
from navigate import extract_fee_summary

# The RIGHT field (price after tax) has a hard cap of 500 EGP on this
# emulator, and it's computed as roughly left * 1.4286 - so the LEFT
# (balance) field maxes out around 500 / 1.4286 =~ 350 EGP. Tiers above
# that get silently rejected by the app before any fee calc happens.
TIERS_TO_TEST = [100, 200, 300]  # EGP balance requested
WAIT_FOR_LIVE_CALC_SECONDS = 2.0


def collect_fee_tiers(tiers=TIERS_TO_TEST):
    results = []

    for tier in tiers:
        print(f"\n--- Testing balance: {tier} EGP ---")

        filled = clear_and_type_field_at_position(str(tier), "left")
        if not filled:
            print(f"Could not fill in {tier} EGP - skipping this tier.")
            continue

        dismiss_keyboard()
        print("Waiting for the live fee calculation to update...")
        time.sleep(WAIT_FOR_LIVE_CALC_SECONDS)

        xml_path = dump_current_screen()
        all_labels = [e["label"] for e in list_interesting_elements(xml_path) if e["label"]]
        summary = extract_fee_summary(all_labels)

        if not summary:
            print(f"No fee breakdown appeared for {tier} EGP - skipping this tier.")
            continue

        row = {"Balance Requested": f"{tier} EGP", **summary}
        results.append(row)
        print(f"  {row}")

    return results


if __name__ == "__main__":
    results = collect_fee_tiers()

    print("\n=== Summary ===")
    for row in results:
        print(row)
