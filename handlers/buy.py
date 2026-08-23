from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import back_keyboard, main_menu_keyboard, packages_keyboard, payment_keyboard
from models import Order, Subscription, User
from packages import DURATION_DAYS, PACKAGES
from vpn_service import VPNPanelService

# Lookup maps for text-based selection
PACKAGE_MAP = {}
for p in PACKAGES:
    key = f"{p['label']} - {p['price']:,} Toomans"
    PACKAGE_MAP[key] = p


async def _get_or_create_user(session, telegram_user) -> User:
    from sqlalchemy import select
    result = await session.execute(select(User).where(User.telegram_id == telegram_user.id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    async with async_session() as session:
        user = await _get_or_create_user(session, update.effective_user)
        order = Order(
            user_id=user.id,
            package_label=pkg["label"],
            duration_days=DURATION_DAYS,
            data_gb=pkg["data_gb"],
            amount_usd=pkg["price"],
            status="pending",
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

    context.user_data["order_id"] = order.id

    payment_text = (
        f"💳 <b>Order #{order.id}</b>\n\n"
        f"📦 Package: {pkg['label']}\n"
        f"📅 Duration: 1 Month\n"
        f"💰 Amount: <b>{pkg['price']:,} Toomans</b>\n\n"
        "─" * 20 + "\n"
        "🏦 <b>Mock Payment Details</b>\n\n"
        f"💳 Card: <code>{config.mock_card_number}</code>\n"
        f"👤 Holder: {config.mock_card_holder}\n"
        f"🪙 Crypto: <code>{config.mock_crypto_wallet}</code>\n\n"
        "⚠️ <i>This is a demo. No real payment is processed.</i>\n\n"
        "Click <b>✅ I have paid</b> when ready:"
    )
    await update.message.reply_text(payment_text, reply_markup=payment_keyboard(), parse_mode="HTML")


async def payment_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("order_id")
    if not order_id:
        await update.message.reply_text("❌ No pending order. Use 🛒 Buy Subscription to start.")
        return

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            await update.message.reply_text("❌ Order not found.")
            return

        if config.auto_approve:
            order.status = "approved"
            await _approve_order(session, order)
            context.user_data.pop("order_id", None)
            await update.message.reply_text(
                f"✅ <b>Payment Confirmed!</b>\n\n"
                f"Order #{order.id} has been approved.\n"
                f"Your VPN config is ready — check <b>👤 My Profile</b>.",
                reply_markup=back_keyboard(),
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"⏳ <b>Order #{order.id} Submitted</b>\n\n"
                "Your payment is pending admin approval.\n"
                "You will be notified once confirmed.",
                reply_markup=back_keyboard(),
                parse_mode="HTML",
            )


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order_id = context.user_data.get("order_id")
    if not order_id:
        await update.message.reply_text("❌ No pending order to cancel.")
        return

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order and order.status == "pending":
            order.status = "rejected"
            await session.commit()

    context.user_data.pop("order_id", None)
    await update.message.reply_text(
        f"❌ Order #{order_id} cancelled.",
        reply_markup=back_keyboard(),
        parse_mode="HTML",
    )


async def _approve_order(session, order: Order) -> Subscription:
    from datetime import timedelta
    vpn_config = VPNPanelService.create_user_config(order.user_id, order.duration_days)
    subscription = Subscription(
        user_id=order.user_id,
        package_label=order.package_label,
        duration_days=order.duration_days,
        data_gb=order.data_gb,
        vpn_config=vpn_config,
        is_active=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=order.duration_days),
    )
    session.add(subscription)
    await session.commit()
    await session.refresh(subscription)
    return subscription
