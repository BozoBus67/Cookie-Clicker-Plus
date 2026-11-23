import os

import stripe
from fastapi import APIRouter, HTTPException, Request

from db.client import supabase

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

router = APIRouter()

@router.post("/stripe_webhook")
async def stripe_webhook(request: Request):
  payload = await request.body()
  sig_header = request.headers.get("stripe-signature")

  try:
    event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
  except Exception:
    raise HTTPException(status_code=400, detail="Invalid signature")

  if event["type"] != "checkout.session.completed":
    return {"status": "ignored", "reason": f"unhandled event type: {event['type']}"}

  event_id = event["id"]

  # Idempotency guard. Stripe retries the webhook on non-2xx responses and on
  # timeouts, so the same event.id can arrive more than once. Processed ids
  # are tracked in Stripe_Processed_Events (event_id TEXT PRIMARY KEY, written
  # only from here), and a replay returns without touching tokens. Check first
  # so a replay short-circuits before any user lookup.
  #
  # Select-then-insert is not strictly atomic, but Stripe spaces retries
  # seconds-to-minutes apart so true simultaneous delivery of the same event
  # is vanishingly rare. The PRIMARY KEY on event_id still catches that
  # corner with a unique-violation if it ever happens, which is why the
  # column has a PK rather than being a plain string.
  existing = supabase.table("Stripe_Processed_Events").select("event_id").eq("event_id", event_id).execute()
  if existing.data:
    return {"status": "already_processed", "event_id": event_id}

  session = event["data"]["object"]
  user_id = getattr(session, "client_reference_id", None)
  amount_total = getattr(session, "amount_total", None)

  if not user_id:
    raise HTTPException(status_code=400, detail="Missing client_reference_id")
  if not amount_total:
    raise HTTPException(status_code=400, detail="Missing amount_total")

  tokens = amount_total // 100
  if tokens <= 0:
    raise HTTPException(status_code=400, detail="Invalid token amount")

  pgd = supabase.table("User_Login_Data").select("premium_game_data").eq("id", user_id).single().execute().data["premium_game_data"]
  pgd["tokens"] = pgd["tokens"] + tokens
  supabase.table("User_Login_Data").update({"premium_game_data": pgd}).eq("id", user_id).execute()

  # Mark processed AFTER the credit so a crash between credit and mark leads
  # to at-worst a retried credit (next delivery sees no row, credits again).
  # The alternative — mark first — would silently drop credit if the mark
  # succeeded but the credit crashed, which costs the user real money for no
  # tokens. Double-credit on a crash is the less-bad trade-off here.
  supabase.table("Stripe_Processed_Events").insert({"event_id": event_id}).execute()

  return {"status": "ok"}
