from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from db.client import supabase
from services.auth import require_user
from services.cookies import add_cookies, spend_cookies
from services.gates import require_min_tier
from services.tokens import add_tokens, spend_tokens

# All auction interactions (create, buy, cancel) are gated to Pro tier (2),
# mirroring the frontend tier-locked modal. Cancel is gated too because tier
# upgrades are one-way in this app — there's no path for a Pro user to drop
# below Pro, so "downgraded user with stale own listings" isn't a real scenario.
_REQUIRE_AUCTION_TIER = require_min_tier(2)

router = APIRouter()

class CreateListingRequest(BaseModel):
  listing_type: Literal["tokens", "cookies"]
  amount: int
  price_type: Literal["tokens", "cookies"]
  price: int

class ListingRequest(BaseModel):
  # Auction_House.id is a Supabase-generated bigint, so the JSON body sends
  # this as a number. Pydantic v2 won't coerce int → str, so typing this as
  # `str` causes a 422 on every buy/cancel.
  listing_id: int

@router.post("/create_listing")
def create_listing(body: CreateListingRequest, user=Depends(_REQUIRE_AUCTION_TIER)):
  if body.amount <= 0 or body.price <= 0:
    raise HTTPException(status_code=400, detail="Amount and price must be positive")

  if body.listing_type == "tokens":
    spend_tokens(user.id, body.amount)
  elif body.listing_type == "cookies":
    spend_cookies(user.id, body.amount)

  user_row = supabase.table("User_Login_Data").select("username").eq("id", user.id).single().execute()
  username = user_row.data["username"]

  result = supabase.table("Auction_House").insert({
    "seller_username": username,
    "selling_item_type": body.listing_type,
    "amount": body.amount,
    "price_item_type": body.price_type,
    "price_item_amount": body.price,
    "creation_timestamptz": datetime.now(timezone.utc).isoformat(),
  }).execute()

  pgd_result = supabase.table("User_Login_Data").select("premium_game_data").eq("id", user.id).single().execute()
  return {"status": "ok", "listing": result.data[0], "premium_game_data": pgd_result.data["premium_game_data"]}

@router.get("/get_listings")
def get_listings():
  result = supabase.table("Auction_House").select("*").execute()
  return result.data

@router.post("/buy_listing")
def buy_listing(body: ListingRequest, user=Depends(_REQUIRE_AUCTION_TIER)):
  listing_result = supabase.table("Auction_House").select("*").eq("id", body.listing_id).single().execute()
  if not listing_result.data:
    raise HTTPException(status_code=404, detail="Listing not found")
  listing = listing_result.data

  seller_row = supabase.table("User_Login_Data").select("id").eq("username", listing["seller_username"]).single().execute()
  seller_id = seller_row.data["id"]

  if str(user.id) == str(seller_id):
    raise HTTPException(status_code=400, detail="Cannot buy your own listing")

  # Atomic reserve. The predicate'd DELETE serializes concurrent buys at the
  # DB layer — only the first DELETE returns a row, the rest see empty .data
  # and get a 409. Without this, two buyers could both pass the SELECT above
  # and both run the full spend/credit flow, duplicating the seller's payment
  # and fabricating the buyer's item from nothing. Same shape as
  # cancel_listing's reserve-via-delete. See BUG_LOG 2026-11-23.
  deleted = supabase.table("Auction_House").delete().eq("id", body.listing_id).execute()
  if not deleted.data:
    raise HTTPException(status_code=409, detail="Listing no longer available")

  # If a spend fails after the reserve (e.g. buyer's balance drained from
  # another tab between the SELECT and here), the listing is already gone,
  # so re-insert it to compensate. The re-insert gets a fresh auto-generated
  # id but preserves everything the seller cares about.
  try:
    _execute_trade(user.id, seller_id, listing)
  except HTTPException:
    _reinsert_listing(listing)
    raise

  buyer_data = supabase.table("User_Login_Data").select("game_data, premium_game_data").eq("id", user.id).single().execute().data
  return {"status": "ok", "listing": listing, "game_data": buyer_data["game_data"], "premium_game_data": buyer_data["premium_game_data"]}

@router.post("/cancel_listing")
def cancel_listing(body: ListingRequest, user=Depends(_REQUIRE_AUCTION_TIER)):
  user_row = supabase.table("User_Login_Data").select("username").eq("id", user.id).single().execute()
  username = user_row.data["username"]

  # Atomic check-and-act: the ownership predicate lives in the DELETE filter so
  # Postgres serializes concurrent cancels — only the first returns a row, the
  # rest see empty .data and noop. Without this, N parallel cancels each pass
  # an independent SELECT check and N refunds fire (token duplication).
  deleted = supabase.table("Auction_House").delete().eq("id", body.listing_id).eq("seller_username", username).execute()
  if not deleted.data:
    raise HTTPException(status_code=409, detail="Listing not found, not owned by you, or already cancelled")
  listing = deleted.data[0]

  if listing["selling_item_type"] == "tokens":
    add_tokens(user.id, listing["amount"])
  elif listing["selling_item_type"] == "cookies":
    add_cookies(user.id, listing["amount"])

  user_data = supabase.table("User_Login_Data").select("game_data, premium_game_data").eq("id", user.id).single().execute().data
  return {"status": "ok", "listing": listing, "game_data": user_data["game_data"], "premium_game_data": user_data["premium_game_data"]}


def _execute_trade(buyer_id, seller_id, listing):
  if listing["price_item_type"] == "tokens":
    spend_tokens(buyer_id, listing["price_item_amount"])
    add_tokens(seller_id, listing["price_item_amount"])
  elif listing["price_item_type"] == "cookies":
    spend_cookies(buyer_id, listing["price_item_amount"])
    add_cookies(seller_id, listing["price_item_amount"])

  if listing["selling_item_type"] == "tokens":
    add_tokens(buyer_id, listing["amount"])
  elif listing["selling_item_type"] == "cookies":
    add_cookies(buyer_id, listing["amount"])


def _reinsert_listing(listing):
  supabase.table("Auction_House").insert({
    "seller_username": listing["seller_username"],
    "selling_item_type": listing["selling_item_type"],
    "amount": listing["amount"],
    "price_item_type": listing["price_item_type"],
    "price_item_amount": listing["price_item_amount"],
    "creation_timestamptz": listing["creation_timestamptz"],
  }).execute()
