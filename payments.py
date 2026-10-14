"""Crypto invoice orchestration. Invoice generation and on-chain reconciliation
belong to the processor; the destination a settled payment is forwarded to is the
operator's own, from the ``SOL``/``LTC``/``BTC``/``ETH``/``USDT``/``USDC`` Railway
variables (see :mod:`wallets`). No private key is loaded and nothing is ever
signed here -- those variables hold a public address or a recovery phrase used
only to derive one.
A USD monthly pass is a one-month purchase, not an automatic crypto debit.
"""
import asyncio
import calendar
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
import accounts
import wallets

log = logging.getLogger("bahs.payments")

PLANS = {"monthly": "8.99", "forever": "45.99"}
# Network is explicit; never silently replace Solana USDT with ERC-20 USDT.
# The payout destination is not here: it is read per checkout from the coin's
# own environment variable, so rotating a wallet is a variable change and never
# a code change.
COINS = {
    "SOL": ("sol", "Solana"),
    "LTC": ("ltc", "Litecoin"),
    "ETH": ("eth", "Ethereum"),
    "BTC": ("btc", "Bitcoin"),
    "USDT": ("usdtsol", "Solana · SPL"),
    "USDC": ("usdc", "Ethereum · ERC-20"),
}
TERMINAL = {"finished", "failed", "refunded"}
router = APIRouter()


def processor_ready():
    # Explicit opt-in: this paid processor does not meet a free-unlimited-RPC
    # constraint. Never enable real-money invoices merely because keys exist.
    return bool(os.getenv("NOWPAYMENTS_ENABLED") == "1" and os.getenv("NOWPAYMENTS_API_KEY") and os.getenv("NOWPAYMENTS_IPN_SECRET"))


def ready():
    """Whether a checkout can start at all: is there a wallet to pay into?

    Where the money lands is required either way -- the processor forwards to it,
    or it is the deposit address on a self-custody invoice. What the processor
    changes is only who confirms the chain, so a deployment with wallets and no
    processor is live, not disabled.
    """
    return bool(wallets.available())


def configured():
    """Alias kept for the callers that only mean "the processor"."""
    return processor_ready()


async def provider(method, path, **kwargs):
    if not processor_ready():
        raise HTTPException(503, "Crypto checkout is not configured yet. No wallet or invoice has been generated.")
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            r = await client.request(method, "https://api.nowpayments.io/v1" + path,
                                     headers={"x-api-key": os.environ["NOWPAYMENTS_API_KEY"]}, **kwargs)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError):
            # Never expose provider errors that can contain credentials/internal details.
            raise HTTPException(502, "Payment network unavailable or this amount/network is unsupported. Try later.")


def get_order(oid):
    with accounts.store.transaction() as db:
        return db.one("SELECT * FROM orders WHERE id=?", (oid,))


def public_order(order):
    now = time.time()
    return {k: order[k] for k in ("id", "plan", "currency", "price", "address", "amount", "created", "expires", "status", "granted", "error")} | {
        "network": COINS[order["currency"]][1], "seconds_left": max(0, int(order["expires"] - now)),
        "expired": now >= order["expires"],
        "late_watch": order["expires"] <= now < order["watch_until"] and not order["granted"],
        "renewal": "manual" if order["plan"] == "monthly" else "none",
        # No provider id means this invoice pays straight into the operator's
        # wallet and is released by hand. The UI has to say so rather than imply
        # a chain watcher that does not exist here.
        "self_custody": not order["provider_id"],
    }


def next_month(timestamp):
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1])).timestamp()


def reconcile(payload):
    """Trust only signed IPN/server poll. An order can grant access exactly once."""
    pid = str(payload.get("payment_id", ""))
    with accounts.store.transaction() as db:
        db.lock("payment:" + pid)
        row = db.one("SELECT * FROM orders WHERE provider_id=?", (pid,))
        if not row:
            return False  # callback can race invoice creation; polling reconciles it
        try:
            match = (str(payload.get("order_id")) == row["id"] and
                     payload.get("price_currency", "").lower() == "usd" and
                     Decimal(str(payload.get("price_amount"))) == Decimal(row["price"]) and
                     payload.get("pay_currency", "").lower() == COINS[row["currency"]][0] and
                     payload.get("pay_address") == row["address"])
        except (InvalidOperation, TypeError):
            match = False
        if not match:
            raise HTTPException(400, "Payment does not match the issued invoice")
        status = payload.get("payment_status", "")
        if status not in {"waiting", "confirming", "confirmed", "sending", "partially_paid", "finished", "failed", "expired", "refunded"}:
            raise HTTPException(400, "Unknown payment state")
        if row["granted"]:
            # Fulfilment never repeats or downgrades on reordered notifications.
            return True
        if status in {"confirming", "confirmed", "sending", "finished", "partially_paid"}:
            db.execute("INSERT INTO payment_receipts VALUES(?,?) ON CONFLICT(order_id) DO NOTHING", (row["id"], time.time()))
        receipt = db.one("SELECT first_seen FROM payment_receipts WHERE order_id=?", (row["id"],))
        if status == "finished":
            try:
                sufficient = Decimal(str(payload.get("actually_paid", "0"))) >= Decimal(row["amount"])
            except InvalidOperation:
                sufficient = False
            if not sufficient:
                status = "partially_paid"
            elif not receipt or receipt["first_seen"] > row["watch_until"]:
                status = "review"
                db.execute("UPDATE orders SET error=? WHERE id=?", ("Payment arrived outside the reconciliation window. Contact support with the invoice ID.", row["id"]))
            else:
                db.lock("entitlement:" + row["user_id"])
                user = db.one("SELECT * FROM users WHERE id=?", (row["user_id"],))
                if row["plan"] == "forever":
                    db.execute("UPDATE users SET lifetime=1 WHERE id=?", (user["id"],))
                else:
                    until = next_month(max(time.time(), user["paid_until"]))
                    db.execute("UPDATE users SET paid_until=? WHERE id=?", (until, user["id"]))
                db.execute("UPDATE orders SET granted=1 WHERE id=?", (row["id"],))
        db.execute("UPDATE orders SET status=? WHERE id=?", (status, row["id"]))
    return True


def issue_self_custody(uid: str, plan: str, currency: str) -> dict:
    """An invoice paid straight to the operator's own wallet.

    Nothing here watches a chain, so nothing here may claim a payment. The order
    opens against the address from the coin's environment variable and stays open
    until an administrator releases it -- which is the honest shape of a
    single-wallet setup. The alternative, flipping it to paid on a timer, would
    hand out access for money that never moved.
    """
    address = wallets.address(currency)
    now, oid = time.time(), uuid.uuid4().hex
    with accounts.store.transaction() as db:
        db.lock("checkout:" + uid)
        existing = db.one("SELECT * FROM orders WHERE user_id=? AND expires>? AND status NOT IN ('failed','refunded','finished') ORDER BY created DESC LIMIT 1", (uid, now))
        if existing:
            if existing["plan"] != plan or existing["currency"] != currency:
                raise HTTPException(409, "An invoice is already open. Complete it or wait for its payment window to expire.")
            return public_order(existing)
        db.execute(
            "INSERT INTO orders(id,user_id,plan,currency,price,created,expires,watch_until,address,amount,status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (oid, uid, plan, currency, PLANS[plan], now, now + 3600, now + 10800, address, PLANS[plan], "awaiting_transfer"),
        )
    return public_order(get_order(oid))


class Checkout(BaseModel):
    plan: str
    currency: str


# --- administration -------------------------------------------------------
# Releasing an entitlement is a deliberate, logged, human act. Nothing here runs
# on a timer and nothing here infers a payment.
class Grant(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    plan: str = Field(pattern="^(monthly|forever|trial|revoke)$")
    order_id: str = Field(default="", max_length=64)


def _target(username: str):
    return username.strip().lower().lstrip("@")


@router.get("/admin/users")
def admin_users(q: str = Query("", max_length=40), limit: int = Query(25, ge=1, le=100)):
    """Accounts by prefix, newest first, each with the plan it currently holds."""
    accounts.require_admin()
    with accounts.store.transaction() as db:
        rows = db.all(
            "SELECT * FROM users WHERE username LIKE ? ORDER BY created DESC LIMIT ?",
            (_target(q) + "%", limit),
        )
    return {"users": [{**accounts.public_user(row), "created": row["created"]} for row in rows]}


@router.post("/admin/grant")
async def admin_grant(body: Grant) -> dict:
    """Give an account a plan: monthly, forever, a fresh trial, or nothing at all.

    Monthly extends from today or from what is left, whichever is later, so
    granting twice never shortens a subscription. Revoking clears every route in
    at once -- paid, lifetime and unstarted trial -- because "revoke" that leaves
    a trial behind is not a revoke.
    """
    admin = accounts.require_admin()
    now = time.time()

    def apply():
        name = _target(body.username)
        with accounts.store.transaction() as db:
            db.lock("entitlement:" + name)
            row = db.one("SELECT * FROM users WHERE username=?", (name,))
            if not row:
                return None
            if body.plan == "monthly":
                db.execute("UPDATE users SET paid_until=? WHERE id=?", (next_month(max(now, row["paid_until"])), row["id"]))
            elif body.plan == "forever":
                db.execute("UPDATE users SET lifetime=1 WHERE id=?", (row["id"],))
            elif body.plan == "trial":
                # Unstarted on purpose: the window is spent snipe-ing, as with any
                # other trial, so this does not hand out an expiring-by-the-clock plan.
                db.execute("UPDATE users SET trial_available=1,trial_started=0,trial_until=0 WHERE id=?", (row["id"],))
            else:
                db.execute("UPDATE users SET paid_until=0,lifetime=0,trial_available=0,trial_started=?,trial_until=0 WHERE id=?", (now, row["id"]))
            if body.order_id:
                # Only the order belonging to this account, and only once.
                db.execute("UPDATE orders SET granted=1,status='finished' WHERE id=? AND user_id=?", (body.order_id, row["id"]))
            updated = db.one("SELECT * FROM users WHERE id=?", (row["id"],))
        log.info("grant: %s -> %s (%s)", admin["username"], name, body.plan)
        return accounts.public_user(updated)

    result = await asyncio.to_thread(apply)
    if result is None:
        raise HTTPException(404, "No account with that username")
    return {"user": result}


@router.get("/admin/orders")
def admin_orders(limit: int = Query(25, ge=1, le=100)) -> dict:
    """Recent invoices with their account, so a manual release starts from one screen."""
    accounts.require_admin()
    with accounts.store.transaction() as db:
        rows = db.all(
            "SELECT o.*, u.username FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.created DESC LIMIT ?",
            (limit,),
        )
    orders = [{**public_order(row), "username": row["username"]} for row in rows]
    return {"orders": orders, "awaiting": sum(1 for row in orders if row["status"] == "awaiting_transfer" and not row["granted"])}


@router.get("/billing/plans")
async def plans():
    """Every coin stays listed; ``ready`` says which ones have a wallet to pay to.

    Deriving an address from a recovery phrase is a few tens of milliseconds of
    pure Python, so a cold first call goes to a worker thread rather than stalling
    the event loop. Results are cached per coin, so only that first call pays it.
    """
    ready = set(await asyncio.to_thread(wallets.available))
    return {"plans": PLANS,
            "coins": [{"symbol": c, "network": v[1], "ready": c in ready} for c, v in COINS.items()],
            "ready": bool(ready), "processor": processor_ready(),
            "settlement": "provider" if processor_ready() else "manual",
            "monthly_renewal": "manual", "window_minutes": 60, "late_watch_minutes": 120}


@router.post("/billing/checkout")
async def checkout(body: Checkout):
    uid = accounts.owner_id()
    if body.plan not in PLANS or body.currency not in COINS:
        raise HTTPException(400, "Unknown plan or currency")
    if not ready():
        raise HTTPException(503, "Checkout setup is pending. No funds can be accepted yet.")
    await asyncio.to_thread(accounts.rate_limit, "checkout:" + uid, 6, 3600)
    coin, network = COINS[body.currency]
    # Resolved before any provider call: an invoice for a coin with no wallet is
    # an invoice nobody can settle, and it must fail before money can move.
    payout = await asyncio.to_thread(wallets.address, body.currency)
    if not processor_ready():
        return await asyncio.to_thread(issue_self_custody, uid, body.plan, body.currency)
    currencies = await provider("GET", "/currencies")
    if coin not in currencies.get("currencies", []):
        raise HTTPException(503, "This currency/network is not currently available")
    # The provider enforces live minimums. Small ETH/BTC orders can be refused;
    # never manufacture an address for an unsupported $8.99 route.
    minimum = await provider("GET", "/min-amount", params={"currency_from": coin, "currency_to": coin})
    estimate = await provider("GET", "/estimate", params={"amount": PLANS[body.plan], "currency_from": "usd", "currency_to": coin})
    try:
        if Decimal(str(estimate["estimated_amount"])) < Decimal(str(minimum["min_amount"])):
            raise HTTPException(400, "This plan is below the network minimum. Choose a lower-fee currency.")
    except (KeyError, InvalidOperation):
        raise HTTPException(502, "Cannot obtain a valid payment quote")
    now, oid = time.time(), uuid.uuid4().hex
    def reserve():
        with accounts.store.transaction() as db:
            db.lock("checkout:" + uid)
            existing = db.one("SELECT * FROM orders WHERE user_id=? AND expires>? AND status NOT IN ('failed','refunded','finished') ORDER BY created DESC LIMIT 1", (uid, now))
            if existing:
                if existing["status"] == "creating":
                    raise HTTPException(409, "An invoice is being generated. Retry shortly.")
                if existing["plan"] != body.plan or existing["currency"] != body.currency:
                    raise HTTPException(409, "An invoice is already open. Complete it or wait for its payment window to expire.")
                return existing
            db.execute("INSERT INTO orders(id,user_id,plan,currency,price,created,expires,watch_until) VALUES(?,?,?,?,?,?,?,?)", (oid, uid, body.plan, body.currency, PLANS[body.plan], now, now + 3600, now + 10800))
        return None
    existing = await asyncio.to_thread(reserve)
    if existing:
        return public_order(existing)
    try:
        data = await provider("POST", "/payment", json={
            "price_amount": float(PLANS[body.plan]), "price_currency": "usd", "pay_currency": coin,
            "order_id": oid, "order_description": "VORTEX " + body.plan,
            "ipn_callback_url": accounts.origin() + "/billing/webhook",
            "payout_address": payout, "payout_currency": coin,
            "is_fixed_rate": True, "is_fee_paid_by_user": True,
        })
        if not data.get("payment_id") or not data.get("pay_address") or Decimal(str(data.get("pay_amount", "0"))) <= 0:
            raise HTTPException(502, "Payment provider did not return a valid invoice")
        def save():
            # Deduplicate by actual chain, not ticker: ETH/USDC share Ethereum,
            # SOL/USDT share Solana. Ledger survives invoice expiry and refunds.
            chain = {"SOL":"solana", "USDT":"solana", "ETH":"ethereum", "USDC":"ethereum", "BTC":"bitcoin", "LTC":"litecoin"}[body.currency]
            address = data["pay_address"].lower() if chain == "ethereum" else data["pay_address"]
            with accounts.store.transaction() as db:
                db.lock("address:" + chain + ":" + address)
                if db.one("SELECT order_id FROM issued_addresses WHERE network=? AND address=?", (chain, address)):
                    raise HTTPException(502, "Provider reused a deposit address. Invoice withheld.")
                db.execute("INSERT INTO issued_addresses VALUES(?,?,?,?)", (chain, address, oid, now))
                db.execute("UPDATE orders SET provider_id=?,address=?,amount=?,status='waiting' WHERE id=?", (str(data["payment_id"]), data["pay_address"], str(data["pay_amount"]), oid))
        await asyncio.to_thread(save)
    except (HTTPException, InvalidOperation):
        # An uncertain network error may have created an invoice remotely. Keep
        # its reserved order ID for support; don't blindly retry a money action.
        with accounts.store.transaction() as db:
            db.execute("UPDATE orders SET status='review',error=? WHERE id=?", ("Invoice creation could not be confirmed. Contact support with this invoice ID before retrying.", oid))
        raise HTTPException(502, "Invoice creation uncertain. Contact support before retrying.")
    return public_order(await asyncio.to_thread(get_order, oid))


@router.get("/billing/orders")
def orders():
    with accounts.store.transaction() as db:
        rows = db.all("SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 30", (accounts.owner_id(),))
    return {"orders": [public_order(r) for r in rows]}


@router.get("/billing/orders/{oid}")
async def order_status(oid: str):
    row = await asyncio.to_thread(get_order, oid)
    if not row or row["user_id"] != accounts.owner_id():
        raise HTTPException(404, "Invoice not found")
    return public_order(row)


@router.post("/billing/webhook")
async def webhook(request: Request):
    key = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
    if not key:
        raise HTTPException(503, "Webhook is not configured")
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, "Payload too large")
    try:
        data = json.loads(raw)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid callback")
    expected = hmac.new(key.encode(), canonical.encode(), hashlib.sha512).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-nowpayments-sig", "")):
        raise HTTPException(401, "Invalid callback signature")
    await asyncio.to_thread(reconcile, data)
    return {"ok": True}


async def monitor():
    """Restart-safe polling, including invoices hidden after their first hour."""
    while True:
        if processor_ready():
            with accounts.store.transaction() as db:
                rows = db.all("SELECT * FROM orders WHERE provider_id IS NOT NULL AND granted=0 AND status NOT IN ('failed','refunded','review') AND (watch_until>? OR id IN (SELECT order_id FROM payment_receipts WHERE first_seen<=orders.watch_until)) AND poll_after<? ORDER BY poll_after LIMIT 20", (time.time(), time.time()))
            for row in rows:
                try:
                    payload = await provider("GET", "/payment/" + row["provider_id"])
                    await asyncio.to_thread(reconcile, payload)
                except HTTPException:
                    # Keep record and retry; never grant when reconciliation fails.
                    pass
                with accounts.store.transaction() as db:
                    db.execute("UPDATE orders SET poll_after=? WHERE id=?", (time.time() + 30, row["id"]))
        await asyncio.sleep(15)
