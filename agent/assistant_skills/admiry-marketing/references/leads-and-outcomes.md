# Leads and what they became

What each lead became shows the owner what a booked customer costs, place by place, and counts toward their goal. Someone has to say it, and you are the easiest place for the owner to do so.

- `get_leads` lists the 50 newest, newest first. With `awaiting_outcome: true` it lists the ones nobody has said the outcome of; `get_marketing_state` says how many wait.
- When the owner says what happened ("the Smith job booked", "she never answered"), `update_lead`: `status` (contacted, booked, won, lost, spam); `value_usd` when a job was won and the owner says what it was worth; `reason` when it was lost or was not a real lead (out_of_area, wrong_service, no_answer, price, duplicate, not_a_customer, other). Marking a lead booked or won clears a reason given earlier.
- When the owner mentions a customer who is not in the list (a phone call, someone who walked in and said they saw the ad), `add_lead`. Look in `get_leads` first, so one customer is not counted twice. `happened_at` is the day it happened in the business's own calendar; `contact` takes `name`, `phone` and `email`, as text.
- Leads the owner reports count for the business, and toward a goal of customers (when won) or bookings (when booked); a goal of calls counts the calls the platforms counted (NewsBreak reports none). They are not credited to any one place the ads run.
- A lead holds a person's name and number. Use them to help the owner follow up; do not repeat them where they are not needed.
- Once a week is a good rhythm: ask the owner which of the waiting leads became jobs.
