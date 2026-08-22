from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from database import async_session
from keyboards import get_back_to_menu_keyboard, get_durations_keyboard, get_packages_keyboard, get_payment_keyboard
from models import Order, Subscription, User
from packages import DURATIONS, PACKAGES
from vpn_service import VPNPanelService


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
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup(
        [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
        for row in get_packages_keyboard(PACKAGES)
    )
    await query.edit_message_text(
        "🛒 <b>Select a Package</b>\n\nChoose your desired data allowance:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pkg_id = int(query.data.split("_")[1])
    pkg = next((p for p in PACKAGES if p["id"] == pkg_id), None)
    if not pkg:
        await query.edit_message_text("❌ Invalid package.")
        return

    context.user_data["selected_package"] = pkg
    keyboard = InlineKeyboardMarkup(
        [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
        for row in get_durations_keyboard(pkg_id, DURATIONS)
    )
    await query.edit_message_text(
        f"📦 <b>{pkg['label']}</b> selected.\n\nChoose subscription duration:",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def duration_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, pkg_id_str, dur_id_str = query.data.split("_")
    pkg_id, dur_id = int(pkg_id_str), int(dur_id_str)

    pkg = next((p for p in PACKAGES if p["id"] == pkg_id), None)
    dur = next((d for d in DURATIONS if d["id"] == dur_id), None)
    if not pkg or not dur:
        await query.edit_message_text("❌ Invalid selection.")
        return

    async with async_session() as session:
        user = await _get_or_create_user(session, query.from_user)
        order = Order(
            user_id=user.id,
            package_label=pkg["label"],
            duration_days=dur["days"],
            data_gb=pkg["data_gb"],
            amount_usd=dur["price_usd"],
            status="pending",
        )
        session.add(order)
        await session.commit()
        await session.refresh(order)

    context.user_data["order_id"] = order.id

    payment_text = (
        f"💳 <b>Order #{order.id}</b>\n\n"
        f"📦 Package: {pkg['label']}\n"
        f"📅 Duration: {dur['label']}\n"
        f"💰 Amount: <b>${dur['price_usd']}</b>\n\n"
        "─" * 20 + "\n"
        "🏦 <b>Mock Payment Details</b>\n\n"
        f"💳 Card: <code>{config.mock_card_number}</code>\n"
        f"👤 Holder: {config.mock_card_holder}\n"
        f"🪙 Crypto: <code>{config.mock_crypto_wallet}</code>\n\n"
        "⚠️ <i>This is a demo. No real payment is processed.</i>\n\n"
        "Click <b>✅ I have paid</b> when ready:"
    )
    keyboard = InlineKeyboardMarkup(
        [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
        for row in get_payment_keyboard(order.id)
    )
    await query.edit_message_text(payment_text, reply_markup=keyboard, parse_mode="HTML")


async def payment_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[1])

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            await query.edit_message_text("❌ Order not found.")
            return

        if config.auto_approve:
            order.status = "approved"
            await _approve_order(session, order)
            await query.edit_message_text(
                f"✅ <b>Payment Confirmed!</b>\n\n"
                f"Order #{order.id} has been approved.\n"
                f"Your VPN config is ready — check <b>👤 My Profile</b>.",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
                    for row in get_back_to_menu_keyboard()
                ),
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"⏳ <b>Order #{order.id} Submitted</b>\n\n"
                "Your payment is pending admin approval.\n"
                "You will be notified once confirmed.",
                reply_markup=InlineKeyboardMarkup(
                    [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
                    for row in get_back_to_menu_keyboard()
                ),
                parse_mode="HTML",
            )


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split("_")[1])

    async with async_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order and order.status == "pending":
            order.status = "rejected"
            await session.commit()

    await query.edit_message_text(
        f"❌ Order #{order_id} cancelled.",
        reply_markup=InlineKeyboardMarkup(
            [InlineKeyboardButton(text=btn, callback_data=data) for btn, data in row]
            for row in get_back_to_menu_keyboard()
        ),
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
