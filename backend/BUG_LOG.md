> AI-generated, proofread and edited (by human).

# Bug Log

Past bugs whose root cause wasn't obvious from the symptom. Add an entry whenever you fix one — the goal is that next time you (or the AI) see the same shape, you spot it fast instead of re-deriving the cause.

Each entry has the symptom, what was actually wrong, the fix, and the pattern to watch for elsewhere in the code.

---

## 2026-11-23 — Auction buy duplication (TOCTOU race) + Stripe webhook idempotency

Two related correctness holes that both let the shared economy get inflated. Fixing them together because they sit next to each other on the money path.

### Auction `buy_listing`

Symptom: Same shape as the 2026-05-18 cancel bug but on the buy side. Two buyers clicking the same listing simultaneously could both pass the existence SELECT, both execute the full spend/credit flow, then one DELETE would succeed and the other would no-op — net effect: seller credited twice, both buyers received the item, cookies/tokens materialized from nothing.

Root cause: TOCTOU race — SELECT-then-side-effects-then-DELETE, with no atomic reserve between the check and the act. The 2026-05-18 entry explicitly flagged this as an unfixed sister bug and noted the reason it wasn't fixed at the same time: buy involves money moving in both directions, so you need rollback handling (the reserve-then-fail compensation) that cancel doesn't.

Fix: Collapsed the reserve into the DELETE itself (`DELETE FROM Auction_House WHERE id = $1 RETURNING *`). Only the first buyer gets the row back; the rest see empty `.data` and get a 409 before any payment moves. For the balance-drained-mid-flight case (buyer passed affordability implicitly at SELECT time but failed at spend time), the spend/credit block is wrapped in try/except and re-inserts the listing on failure so the seller's item isn't destroyed. See `_execute_trade` and `_reinsert_listing` in `routers/auction_house.py`.

### Stripe webhook double-credit

Symptom: Stripe retries `checkout.session.completed` on any non-2xx response or timeout. A transient 502 from Render, a slow Supabase round-trip, or a cold-start on the webhook handler all led to the same event.id being delivered more than once, and each delivery credited the tokens again.

Root cause: No idempotency key. The handler trusted that each delivery was unique and credited unconditionally.

Fix: Added a `Stripe_Processed_Events` table with `event_id TEXT PRIMARY KEY` (needs to be created in Supabase — see follow-ups below). The handler now SELECTs for `event_id` first and returns `already_processed` on a hit, then INSERTs the id after crediting on a miss. Mark-after-credit (not mark-before-credit) was deliberate: a crash between the credit and the mark results in a retried credit (double-credit, which Kevin absorbs), while a crash between the mark and the credit would silently drop the credit (the user pays and gets nothing, which is strictly worse). Trade-off documented inline.

### Watch for this pattern when

- Any endpoint on the money path has a sequence of "read, do side effects, then commit the state change." If the commit isn't what gates the side effects, concurrent callers race.
- Any webhook handler that credits anything. Webhooks are retried, and the retry has to be a no-op.

### Follow-ups required outside the code

- Create the `Stripe_Processed_Events` table in Supabase manually (same place you created the other tables):
  ```sql
  CREATE TABLE "Stripe_Processed_Events" (event_id TEXT PRIMARY KEY);
  ```
  Without this, the webhook's first call tries to SELECT from a table that doesn't exist and 500s. The table is write-only from this handler, so no other callers to worry about.

---

## 2026-05-18 — Auction cancel refund duplication (TOCTOU race)

Symptom: Player created a listing, then fired ~30 cancel requests in parallel — received 30 refunds of the same listed items, duplicating tokens.

Root cause: TOCTOU (time-of-check / time-of-use) race in `routers/auction_house.py:cancel_listing`. The handler did SELECT-to-check-existence-then-SELECT-to-check-ownership, then refunded via `add_tokens` / `add_cookies`, then DELETE'd. With 30 concurrent requests, all 30 hit the SELECTs before any reached the DELETE, so all 30 saw the listing as still active and owned by them, all 30 refunded, then all 30 attempted the DELETE (29 affected zero rows, but the refunds had already fired).

Fix: Collapsed the existence check, ownership check, and state transition into a single atomic SQL DELETE — `DELETE FROM Auction_House WHERE id = $1 AND seller_username = $2 RETURNING *`. Postgres serializes DELETEs at the row level via implicit row locks, so only the first cancel returns a row and refunds; subsequent cancels see empty `result.data` and 409 without refunding. The check and the act are now one indivisible operation; there is no window between them.

Watch for this pattern when:

- An endpoint reads a row, checks something about it, then writes back / deletes based on the check.
- The "check" requires the row to be in a specific state (active, available, owned by X, unsold, etc.) and the "act" transitions the row out of that state.
- The same endpoint can fire multiple times in parallel for the same row — either by a malicious client (as here) or by a buggy frontend that retries.

Same shape still exists in `buy_listing` (lines 63–91) — two concurrent buyers can both claim the same listing, duplicating the bought item on the buyer side. Not fixed in this commit because the buyer-pays-then-receives sequence needs rollback handling (if you spend-then-claim and lose the claim, you must refund; if you claim-then-spend and the buyer is broke, you must reinsert the listing). Treat as a separate fix.

Difference from the 2026-05-09 entry: that was *lost update* (read-modify-write race losing increments on a JSON blob), this is *TOCTOU* (check-then-act race firing a side effect N times). Both are races, different shapes — lost update is fixed with a lock or atomic increment, TOCTOU is fixed by making the check and the act one atomic operation (the conditional DELETE here).

---

## 2026-05-09 — Lost streak tokens on simultaneous checkin (race condition)

Symptom: All three reward popups fired (daily 25 + hourly 5 + fivemin 1 = 31 expected) but the token bar showed only `1`. Refresh didn't help — the database itself only had 1 token.

Root cause: Classic read-modify-write race (also called a "lost update"). On mount, the frontend fires `/daily_checkin`, `/hourly_checkin`, and `/fivemin_checkin` in parallel — no `await` between them. Each handler in `routers/tokens.py` did:

1. read the entire `premium_game_data` JSON blob from Postgres
2. compute `pgd["tokens"] = pgd["tokens"] + tokens_granted` locally
3. write the entire blob back

All three requests read `tokens=0` at near-the-same instant, each computed `0 + N` in isolation, then each blindly overwrote the same row with its own snapshot. The last writer wins. Fivemin happened to be last, so the user kept its `1` — the daily 25 and hourly 5 were silently overwritten. The popups were misleading: they reflect what the response *said* was granted, not what survived in the database.

Fix: Per-user `threading.Lock` in `routers/tokens.py` wrapping the read-modify-write of each endpoint (commit `9915716`). FastAPI sync-def endpoints run in a threadpool, so a `threading.Lock` keyed by `user.id` correctly serializes the trio within the process. Each handler now reads fresh state because the previous one has already committed.

Watch for this pattern when:

- Any endpoint reads a JSON column, mutates a field, and writes the column back. In this repo that means anything touching `premium_game_data` or `game_data` (`routers/buildings.py`, `routers/gamble.py`, `routers/account_tiers.py`, `routers/auction_house.py`, etc.). If two such endpoints can fire concurrently for the same user, they have the same bug.
- The frontend fires multiple endpoints in parallel (no `await` between them) that touch the same row.
- A user reports "I got the success popup but the count is wrong." The popup runs off the response payload, not the persisted state — so the response can lie by the time it's read.

Limits of the current fix: `threading.Lock` only covers a single backend process. If you ever scale to multiple uvicorn workers or multiple Render instances, the race comes back, because each process has its own lock dict. Real fix at that point is to push the atomicity into Postgres — either a `SELECT ... FOR UPDATE` inside a transaction, or rewrite the increments as `UPDATE … SET premium_game_data = jsonb_set(premium_game_data, '{tokens}', …)` so the read-modify-write happens server-side. Not worth doing today; the app runs single-instance.

Is this well known? Yes — extremely. It's one of the canonical concurrent-programming bugs, with a few standard names depending on context:

- **Lost update** — what databases call it.
- **Read-modify-write race** — what systems people call it.
- **Time-of-check / time-of-use (TOCTOU)** — the security flavor of the same shape, where the gap between checking a value and acting on it lets an attacker change it.

Standard solutions, in increasing strength:

1. Serialize the callers (frontend `await` chain, single writer queue) — easiest, breaks if anything bypasses your serializer.
2. Per-process lock (what we did) — fine for single-instance.
3. Database row lock (`SELECT … FOR UPDATE` in a transaction) — correct across processes/instances.
4. Atomic update at the column level (`SET col = col + N`, or `jsonb_set` in one statement) — best when the field is a simple counter; no lock needed.
5. Optimistic concurrency (version column + CAS retry) — best when conflicts are rare and locking would hurt throughput.

The reason it's so common: an in-memory variable looks the same as a database row from the code's perspective, but only one of them is actually exclusive to the current execution. A read followed by a write *looks* atomic in the source; it isn't.
