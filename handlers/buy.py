import logging
from html import escape

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import (
    cancel_keyboard,
    home_keyboard,
    main_menu_keyboard,
    packages_keyboard,
)
from models import Order, User
from packages import DURATION_DAYS, PACKAGES
from vpn_service import VPNPanelError, VPNPanelService
from zarinpal import ZarinpalError, request_payment, verify_payment

log = logging.getLogger(__name__)

# Lookup map for text-based package selection
PACKAGE_MAP = {
    f"{p['label']} - {p['price']:,} Toomans": p for p in PACKAGES
}


class OrderAlreadyApproved(Exception):
    """Raised when another handler approved the order first."""


class OrderNotApprovable(Exception):
    """Raised when trying to fulfill an order that is not pending
    (e.g. cancelled or rejected before the payment landed)."""

    def __init__(self, order_id: int, status: str | None):
        self.status = status
        super().__init__(f"Order #{order_id} is '{status}', not payable")


def purchase_blocked_reason(telegram_id: int) -> str | None:
    """Return a user-facing reason string if this user may not buy right now."""
    if not config.zarinpal_configured:
        return (
            "💳 Payments are temporarily unavailable.\n"
            "Please contact support."
        )
    if config.zarinpal_sandbox and telegram_id not in config.admin_ids:
        return (
            "🔒 <b>Test mode</b> — purchases are currently limited "
            "to the shop owner."
        )
    return None


async def get_or_create_user(session, telegram_user) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_user.id))
    user = result.scalar_one_or_none()
    if user is not None:
        if user.username != telegram_user.username or user.first_name != telegram_user.first_name:
            user.username = telegram_user.username
            user.first_name = telegram_user.first_name or user.first_name
            await session.commit()
        return user

    user = User(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:  # concurrent insert raced us — reuse the winner's row
        await session.rollback()
        result = await session.execute(select(User).where(User.telegram_id == telegram_user.id))
        user = result.scalar_one_or_none()
        if user is None:
            raise
    return user


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocked = purchase_blocked_reason(update.effective_user.id)
    if blocked:
        await update.message.reply_text(blocked, parse_mode="HTML")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "🛒 <b>Select a Package</b>\n\nAll subscriptions are for 1 month:",
        reply_markup=packages_keyboard(),
        parse_mode="HTML",
    )


async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    pkg = PACKAGE_MAP.get(text)
    if not pkg:
        await update.message.reply_text("❌ Invalid package. Please select from the keyboard.")
        return

    blocked = purchase_blocked_reason(update.effective_user.id)
    if blocked:
        await update.message.reply_text(blocked, parse_mode="HTML")
        return

    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user)
        # Supersede earlier pending orders so they can't stack up.
        await session.execute(
            sa_update(Order)
            .where(Order.user_id == user.id, Order.status == "pending")
            .values(status="cancelled")
        )
        order = Order(
            user_id=user.id,
            package_label=pkg["label"],
            duration_days=DURATION_DAYS,
            data_gb=pkg["data_gb"],
            amount_toomans=pkg["price"],
            status="pending",
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

    context.user_data["order_id"] = order.id

    # Real gateway flow: create a payment session and hand the user the link.
    try:
        pay = await request_payment(
            order.id, pkg["price"], f"VardenProxy subscription — {pkg['label']} (order #{order.id})"
        )
    except ZarinpalError as exc:
        log.warning("Payment request for order #%s failed: %s", order.id, exc)
        async with async_session() as session:
            await session.execute(
                sa_update(Order).where(Order.id == order.id).values(status="cancelled")
            )
            await session.commit()
        context.user_data.pop("order_id", None)
        await update.message.reply_text(
            "❌ <b>Could not start the payment.</b>\n"
            "Please try again in a few minutes.\n"
            f"<code>{escape(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    async with async_session() as session:
        await session.execute(
            sa_update(Order)
            .where(Order.id == order.id)
            .values(payment_authority=pay["authority"])
        )
        await session.commit()

    separator = "─" * 20
    gateway_text = (
        f"💳 <b>Order #{order.id}</b>\n\n"
        f"📦 Package: {escape(pkg['label'])}\n"
        f"📅 Duration: 1 Month\n"
        f"💰 Amount: <b>{pkg['price']:,} Toomans</b>\n\n"
        f"{separator}\n"
        "Tap the button below to pay securely via <b>Zarinpal</b>.\n"
        "✅ Your subscription is activated automatically right after payment — "
        "no confirmation needed."
    )
    pay_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("💳 پرداخت با زرین‌پال", url=pay["startpay_url"])]]
    )
    await update.message.reply_text(gateway_text, reply_markup=pay_keyboard, parse_mode="HTML")
    await update.message.reply_text(
        "⏳ Waiting for your payment — we detect it automatically.\n"
        "You can cancel the order meanwhile:",
        reply_markup=cancel_keyboard(),
        parse_mode="HTML",
    )


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("order_id")
    if not order_id:
        await update.message.reply_text("❌ No pending order to cancel.")
        return

    async with async_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order and order.status == "pending":
            order.status = "cancelled"
            await session.commit()

    context.user_data.pop("order_id", None)
    await update.message.reply_text(
        f"❌ Order #{order_id} cancelled.",
        reply_markup=home_keyboard(),
        parse_mode="HTML",
    )


async def verify_and_fulfill_order(session, order: Order) -> dict:
    """Verify the Zarinpal transaction server-side and provision the VPN.

    Returns {"ref_id", "card_pan", "already_done"} on success.
    Raises:
        ZarinpalError        — payment not verified (unpaid/cancelled/gateway error)
        OrderAlreadyApproved — another handler claimed it first
        VPNPanelError        — paid, but panel provisioning failed (order stays
                               pending so a retry can complete it)
    """
    if not order.payment_authority:
        raise ZarinpalError("Order has no payment authority.")
    if order.status == "approved":
        raise OrderAlreadyApproved(order.id)
    if order.status != "pending":
        raise OrderNotApprovable(order.id, order.status)
    outcome = await verify_payment(order.payment_authority, order.amount_toomans)
    await approve_order(session, order)
    # approve_order committed; persist the reference separately (cosmetic).
    order.payment_ref_id = str(outcome["ref_id"]) if outcome["ref_id"] is not None else None
    session.add(order)  # re-attach in case approve_order's rollback detached it
    try:
        await session.commit()
    except Exception:  # ref persistence must never fail an approved order
        log.warning("Could not store payment_ref_id for order #%s", order.id, exc_info=True)
    return outcome


async def approve_order(session, order: Order) -> dict:
    """Atomically approve an order and provision its panel client.

    Returns {"email", "sub_id", "links"}.

    Only 'pending' orders can be claimed: cancelled/rejected orders are
    never fulfilled, even if money for them arrives afterwards.

    Raises OrderAlreadyApproved when another handler claimed it first,
    OrderNotApprovable when the order is not pending, and VPNPanelError on
    panel failures (the claim is reverted to pending).
    Persistence uses explicit UPDATEs so it works even if ``order`` is not
    attached to ``session``; the instance is kept in sync for the caller.
    """
    # Snapshot identifiers up front: rollback() expires instance attributes,
    # and reading them afterwards would trigger an illegal sync lazy-load.
    order_id = order.id
    claim = await session.execute(
        sa_update(Order)
        .where(Order.id == order_id, Order.status == "pending")
        .values(status="approved")
    )
    if claim.rowcount == 0:
        current = (await session.execute(
            select(Order.status).where(Order.id == order_id)
        )).scalar_one_or_none()
        if current == "approved":
            raise OrderAlreadyApproved(order_id)
        raise OrderNotApprovable(order_id, current)
    await session.commit()
    order.status = "approved"

    async def _set(**values):
        await session.execute(sa_update(Order).where(Order.id == order_id).values(**values))
        await session.commit()

    try:
        email = order.panel_email
        sub_id = order.sub_id or ""
        links = []
        if email:
            # Partial provisioning happened before — try to reuse the client.
            links = await VPNPanelService.get_client_links(email) or \
                await VPNPanelService.get_subscription_links(sub_id)
            if links:
                log.info("Order #%s: reusing existing panel client %s", order_id, email)
            else:
                # Client vanished from the panel — drop the stale reference.
                log.warning(
                    "Order #%s: panel client %s has no links; provisioning fresh.",
                    order_id, email,
                )
                await _set(panel_email=None, sub_id=None)
                order.panel_email = None
                order.sub_id = None

        if not links:
            result = await session.execute(select(User).where(User.id == order.user_id))
            user = result.scalar_one()
            panel = await VPNPanelService.create_client(
                user.telegram_id, order.duration_days, order.data_gb
            )
            email = panel["email"]
            sub_id = panel["sub_id"]
            links = panel["links"]
            await _set(panel_email=email, sub_id=sub_id)
            order.panel_email = email
            order.sub_id = sub_id
    except VPNPanelError:
        await session.rollback()
        await session.execute(
            sa_update(Order)
            .where(Order.id == order_id, Order.status == "approved")
            .values(status="pending")
        )
        await session.commit()
        raise

    return {"email": email, "sub_id": sub_id, "links": links}


def format_vpn_config(links: list[str]) -> str:
    """Format the config block (vless URIs) for a message."""
    return "\n".join(f"🔗 <code>{escape(link)}</code>" for link in links if link)
