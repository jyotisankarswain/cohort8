# PITCH.md

Six lines and a lever. Your words. The last two are scored.

Built: A Claude tool-use agent (agent.py loop) with 19 tools: the 9 given, 8 we added in-process (reopen_stats, care_entitlements, hold_time_left, what_automation_will_not_do, cause_in_plain_words, departures_in_window, ground_stop_status, seats_left) and 2 served over MCP by support/mcp_server.py (next_available_day, fare_rules).
Does: For a disrupted-flight PNR (checked on G2HL9V and R8KD3F) it looks up the booking, reads the live flight status, applies the policy row (check_policy) and tells the customer what they are owed and their options, asking before it changes anything.
Number: Tool-list cost per turn went from 3,236 tokens (11 tools, saved at gate 2.1 in .workshop/build2_tokens.json) to 6,077 tokens (19 tools, counted on the wire by `python run.py --tool-tax`, 2026-11-19); the 2 MCP tools cost the same either way (+2,841 tokens, +88%, on every turn of every chat).
Safety check: On G2HL9V (group booking) run.py --trace on 2026-11-19 shows the agent called lookup_booking, get_flight_status, then escalate_to_human (queue GRP1, ESC-9153F4) and made no rebooking, voucher or compensation call; the reply says nothing on the booking was changed.
Next: Author TONE_ADDENDUM (Build 4.1) in our own words for abusive messages, then write evals/cases.json cases (none exist yet; only cases.example.json) and run eval_harness.py so we can prove it before and after.
Still broken: R8KD3F, the abusive-message ticket, still gets the same calm, routine policy answer as any delay (re-run 2026-11-19), because TONE_ADDENDUM is empty in agent.py; nothing in the agent handles or flags the abuse.
Lever: <cost | speed | intelligence>

## Priya asked

Costs:
Wrong:
Runs it:
Left out:
