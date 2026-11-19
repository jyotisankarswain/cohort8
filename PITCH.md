# PITCH.md

Six lines and a lever. Your words. The last two are scored.

Built: A chat agent for Larkspur customers whose flight is disrupted, using Larkspur's nine tools, eight more tools our team wrote (seats_left, reopen_stats, care_entitlements, hold_time_left, what_automation_will_not_do, cause_in_plain_words, departures_in_window, ground_stop_status), and next_available_day and fare_rules served from an MCP server.
Does: It looks up the booking, checks the flight's real status, works out what the policy gives the customer, offers other flights with seats left, and explains why something is or is not covered by quoting the Handbook.
Number: 6,002 to 6,077 tokens per turn for the tool list, before and after moving two tools to MCP; moving them did not make them cheaper.
Safety check: It only places a 15-minute hold and tells the customer to click Confirm themselves; confirm_rebooking never fired in our runs (trace for K7PQ2M), and the group booking G2HL9V went to a person with no vouchers issued.
Next: Have the agent notice an angry or threatening customer and respond to that, instead of answering as if nothing were wrong.
Still broken: On the unclear missed connection (T9WN4C) the agent never asks the customer a single question; it treats it as a plain 150-minute delay.
Lever: intelligence

## Priya asked

Costs:
Wrong:
Runs it:
Left out:
