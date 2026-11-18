The question

Be ready to share your answer in the main room.

If the agent answers wrong, how would you find out before the customer complains? And what would you look at to understand why?




1 step
1 step
Here's a short answer you could share in the main room, based on the Larkspur brief.

Short version: We'd automatically check every conversation against Larkspur's three rules, and keep a full record of each chat. That way we spot mistakes ourselves, and we can trace each one back to why it happened.

1. How we'd find out before the customer complains

We wouldn't wait for complaints. We'd run automatic checks on every live chat, one for each rule:
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
Never make things up: Every flight time, seat and price in a reply gets compared with what Larkspur's systems actually returned in that chat. If a number appears in the reply but not in the data, it gets flagged.
The customer decides: If the agent cancels or changes a booking without a confirm click first, that sets off an alert straight away. Since breaking a rule even once is a dealbreaker, this has to be treated as an emergency, not a weekly report.
Always polite: A second model scores the tone of every reply, so angry chats like R8KD3F get checked. The brief says "nothing checks the tone yet," so this is a gap we'd close.
Handoffs: We'd check that cases the agent can't handle, like the 12-person group (G2HL9V), actually get passed to a person.
We'd also watch for indirect warning signs:

A customer contacts Larkspur again within 24–48 hours about the same booking.
A human agent reverses a rebooking.
A customer gives up halfway through a chat.
A customer leaves a thumbs-down.
On top of that, people would review a regular sample of chats.

Before any change to the prompt or model goes live, we'd rerun a test set built from the five ticket types. That catches new problems before customers see them.

2. What we'd look at to understand why

We'd look at the full record of the conversation: the customer's message, which prompt and model version was running, every lookup the agent made, the exact data that came back, and the agent's final reply.

That tells us where it went wrong:

Bad data: Larkspur's system sent back wrong or missing information.
Missing policy: The agent didn't have the rule it needed, like the delay rule for M3XR8T.
Unclear instructions: The prompt was vague, for example about when to ask a question first (T9WN4C).
Bad judgment: The model had the right information but made the wrong call.
Each mistake then becomes a new test case, so once it's fixed it stays fixed.

One line for the room: Check every chat against the three rules automatically, keep a full record of what the agent saw and did, and turn every mistake into a test.