from ai_pick import classify_screen_and_pick_action

goal = "Navigate to the Mobile Top-Up flow for the e& operator, so we can see the Service Fees for a top-up."
all_labels = ["Mobile", "Electricity", "Water", "Gas", "Landline", "Donations"]
candidates = ["Mobile"]  # only this one is on the guardrail's allowlist

result = classify_screen_and_pick_action(goal, all_labels, candidates)
print(result)