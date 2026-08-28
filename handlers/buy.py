import logging
from html import escape

try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import config
from database import async_session
from handlers.rate_limit import check_cooldown
from keyboards import (
    main_menu_keyboard,
    packages_keyboard,
)
from models import Order, User
from packages import DURATION_DAYS
from vpn_service import VPNPanelError, VPNPanelService
from zarinpal import ZarinpalError, verify_payment

log = logging.getLogger(__name__)

PAYMENT_EXPIRY_SECONDS = 15 * 60  # 15 minutes


def is_order_expired(order) -> bool:
    """Return True if a pending order's payment window has elapsed."""
    if not getattr(order, "created_at", None):
        return False
    created = order.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    try:
        return (datetime.now(UTC) - created).total_seconds() > PAYMENT_EXPIRY_SECONDS
    except Exception:
        return False


async def expire_pending_orders(session) -> int:
    """Cancel all pending orders older than PAYMENT_EXPIRY_SECONDS. Returns count."""
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=PAYMENT_EXPIRY_SECONDS)
    # Collect IDs first to release discount codes after cancel
    try:
        ids_res = await session.execute(
            select(Order.id).where(
                Order.status == "pending",
                Order.created_at < cutoff,
                Order.payment_ref_id.is_(None),
            )
        )
        expired_ids = [r[0] for r in ids_res.all()]
    except Exception:
        expired_ids = []
    result = await session.execute(
        sa_update(Order)
        .where(
            Order.status == "pending",
            Order.created_at < cutoff,
            # Orders marked "provisioning" have verified money attached and
            # must NOT be auto-cancelled — an admin retry completes them.
            Order.payment_ref_id.is_(None),
        )
        .values(status="cancelled")
    )
    await session.commit()
    # Release discount codes for expired orders
    if expired_ids:
        try:
            from handlers.discount import release_discount_codes_for_cancelled_orders

            async with async_session() as rs:
                await release_discount_codes_for_cancelled_orders(rs, expired_ids)
        except Exception:
            log.warning("Failed to release discount codes for expired orders %s", expired_ids, exc_info=True)
    return result.rowcount or 0


async def cancel_all_pending_for_user(
    telegram_id: int,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
) -> list[int]:
    """Cancel *all* pending orders for telegram_id (DB-driven, survives restart).

    Returns list of cancelled order IDs (empty if none). Also best-effort
    strips the inline pay button from the previous pay message and clears
    context.user_data pending keys. Atomic: only pending → cancelled.
    """
    cancelled_ids: list[int] = []
    try:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user is None:
                return []
            result = await session.execute(
                select(Order.id).where(Order.user_id == user.id, Order.status == "pending")
            )
            ids = [r[0] for r in result.all()]
            if not ids:
                return []
            await session.execute(
                sa_update(Order)
                .where(Order.user_id == user.id, Order.status == "pending")
                .values(status="cancelled")
            )
            await session.commit()
            cancelled_ids = ids
            # Release discount codes for cancelled orders
            try:
                from handlers.discount import release_discount_codes_for_cancelled_orders

                await release_discount_codes_for_cancelled_orders(session, ids)
            except Exception:
                log.warning("Failed to release discount codes for cancelled %s", ids, exc_info=True)
    except Exception:
        log.warning("cancel_all_pending_for_user failed for %s", telegram_id, exc_info=True)
        return []

    # Best-effort: strip the Zarinpal pay inline button so it can't be paid after cancel
    if context is not None and chat_id is not None:
        pay_message_id = None
        try:
            pay_message_id = context.user_data.get("pay_message_id") if hasattr(context, "user_data") else None
        except Exception:
            pay_message_id = None
        if pay_message_id:
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=pay_message_id,
                    reply_markup=None,
                )
            except TelegramError as exc:
                log.info("Could not strip pay button after auto-cancel %s: %s", cancelled_ids, exc)
            except Exception:
                pass
        # Clear RAM pending markers (authoritative state is DB, but keep RAM consistent)
        try:
            if hasattr(context, "user_data"):
                context.user_data.pop("order_id", None)
                context.user_data.pop("pay_message_id", None)
        except Exception:
            pass
    return cancelled_ids


# Lookup map for text-based package selection — refreshed dynamically via packages.load_packages()
def _build_package_map() -> dict[str, dict]:
    import packages as _pkg

    pkgs = _pkg.load_packages()[0]
    return {f"{p['label']} - {p['price']:,} تومان": p for p in pkgs}


PACKAGE_MAP = _build_package_map()


def _get_package_map() -> dict[str, dict]:
    # Live reload so admin panel changes apply without restart
    try:
        return _build_package_map()
    except Exception:
        return PACKAGE_MAP


class OrderAlreadyApproved(Exception):
    """Raised when another handler approved the order first."""


class OrderNotApprovable(Exception):
    """Raised when trying to fulfill an order that is not pending
    (e.g. cancelled or rejected before the payment landed)."""

    def __init__(self, order_id: int, status: str | None):
        self.status = status
        super().__init__(f"Order #{order_id} is '{status}', not payable")


class RenewalLimitExceeded(OrderNotApprovable):
    """Renewal would push total subscription time past MAX_SUBSCRIPTION_DAYS.

    Structured marker so the payment callback auto-refunds only genuine
    over-limit cases instead of string-matching "60"/"exceed" in error text
    (panel emails/order ids can contain those substrings by chance).
    """


def purchase_blocked_reason(telegram_id: int) -> str | None:
    """Return a user-facing reason string if this user may not buy right now."""
    # Check maintenance mode (admin toggled via panel) — file-backed, live reload
    # When payments are paused, only admins may still purchase (new package or
    # renewal). In sandbox mode this also means only admins can use the
    # sandbox gateway; normal users are blocked in both cases.
    try:
        import packages as _pkg

        _, _, _paused = _pkg.load_packages()[:3]
        if _paused and telegram_id not in config.admin_ids:
            return "⏸ سرویس در حال به‌روزرسانی است — لطفاً چند دقیقه بعد دوباره تلاش کنید."
    except Exception:
        pass
    if not config.zarinpal_configured:
        return "💳 پرداخت‌ها موقتاً در دسترس نیستند.\nلطفاً بعداً تلاش کنید یا با پشتیبانی تماس بگیرید."
    if config.zarinpal_sandbox and telegram_id not in config.admin_ids:
        return "🔒 <b>حالت آزمایشی</b> — فعلاً فقط مدیر فروشگاه امکان خرید دارد."
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
    if not await check_cooldown(update.effective_user.id, "buy_start", 5):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
        return
    blocked = purchase_blocked_reason(update.effective_user.id)
    if blocked:
        await update.message.reply_text(blocked, parse_mode="HTML")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "🛒 <b>انتخاب پکیج</b>\n\n"
        "اشتراک‌ها یک‌ماهه هستند؛ یکی را انتخاب کنید:",
        reply_markup=packages_keyboard(),
        parse_mode="HTML",
    )


async def package_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_cooldown(update.effective_user.id, "package_selected", 8):
        await update.message.reply_text("⏳ لطفاً کمی صبر کنید و دوباره تلاش کنید.")
        return
    text = (update.message.text or "").strip()
    pkg = _get_package_map().get(text)
    if not pkg:
        await update.message.reply_text("❌ پکیج نامعتبر است؛ لطفاً از دکمه‌های زیر استفاده کنید.")
        return

    blocked = purchase_blocked_reason(update.effective_user.id)
    if blocked:
        await update.message.reply_text(blocked, parse_mode="HTML")
        return

    async with async_session() as session:
        user = await get_or_create_user(session, update.effective_user)
        # Supersede earlier pending orders so they can't stack up — capture ids to release discount codes
        pending_ids_res = await session.execute(
            select(Order.id).where(Order.user_id == user.id, Order.status == "pending")
        )
        pending_ids = [r[0] for r in pending_ids_res.all()]
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
        if pending_ids:
            try:
                from handlers.discount import release_discount_codes_for_cancelled_orders

                await release_discount_codes_for_cancelled_orders(session, pending_ids)
            except Exception:
                log.warning("Failed to release discount codes for superseded orders %s", pending_ids, exc_info=True)

    # Ask about discount code BEFORE generating the gateway (resume on choice)
    from handlers.discount_flow import send_discount_prompt

    await send_discount_prompt(update, context, order)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id if update.effective_user else None
    order_id = context.user_data.get("order_id")
    order = None

    # Try context first
    if order_id:
        async with async_session() as session:
            result = await session.execute(select(Order).where(Order.id == order_id))
            order = result.scalar_one_or_none()
            if not order or order.status != "pending":
                order = None
                order_id = None

    # Fallback: latest pending for this user (covers restart / RAM loss / orphan)
    if order is None and telegram_id:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()
            if user:
                result = await session.execute(
                    select(Order)
                    .where(Order.user_id == user.id, Order.status == "pending")
                    .order_by(Order.created_at.desc())
                    .limit(1)
                )
                order = result.scalar_one_or_none()
                if order:
                    order_id = order.id

    if not order_id or not order:
        await update.message.reply_text(
            "❌ سفارش در انتظاری برای لغو وجود ندارد.",
            reply_markup=main_menu_keyboard(),
        )
        return

    async with async_session() as session:
        # Atomic claim: only cancel while still pending. A read-then-write
        # here races the payment callback (verify → claim 'approved' →
        # provision): an unconditional UPDATE could overwrite a paid,
        # provisioned order back to 'cancelled'. Same guarded pattern as
        # approve_order's claim.
        claim = await session.execute(
            sa_update(Order)
            .where(Order.id == order_id, Order.status == "pending")
            .values(status="cancelled")
        )
        if claim.rowcount == 0:
            await update.message.reply_text(
                "❌ سفارش در انتظاری برای لغو وجود ندارد.",
                reply_markup=main_menu_keyboard(),
            )
            return
        await session.commit()
        # Reload the cancelled row so the discount release sees the bound code.
        result = await session.execute(select(Order).where(Order.id == order_id))
        db_order = result.scalar_one_or_none()
        if db_order:
            try:
                from handlers.discount import release_discount_code_by_order

                await release_discount_code_by_order(session, db_order)
            except Exception:
                log.warning("Failed to release discount code for cancelled order #%s", order_id, exc_info=True)

    # Best-effort: kill the Zarinpal pay button in chat so the cancelled
    # order can't be paid from a still-visible link.
    pay_message_id = context.user_data.pop("pay_message_id", None)
    if pay_message_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=update.effective_chat.id,
                message_id=pay_message_id,
                reply_markup=None,
            )
        except TelegramError as exc:
            log.info("Could not strip pay button for order #%s: %s", order_id, exc)

    context.user_data.pop("order_id", None)
    context.user_data.pop("awaiting_discount_code", None)
    context.user_data.pop("awaiting_discount_choice", None)
    context.user_data.pop("pending_order_id", None)
    await update.message.reply_text(
        f"❌ سفارش #{order_id} لغو شد.",
        reply_markup=main_menu_keyboard(),
    )


async def renew_order(session, order: Order) -> dict:
    """Atomically claim a renewal order and extend its referenced panel client.

    Returns {"email", "sub_id", "links"}. Mirrors approve_order's claim/revert
    semantics but calls VPNPanelService.extend_client instead of create_client.
    """
    order_id = order.id
    # Gifts are not renewable — defense in depth even if callback was bypassed
    if order.renew_email:
        try:
            _gift_check = await session.execute(
                select(Order.id).where(Order.panel_email == order.renew_email, Order.is_gift == True)  # type: ignore[attr-defined]
            )
            if _gift_check.scalar_one_or_none() is not None:
                raise OrderNotApprovable(order_id, "gift not renewable — renew_email is a gift subscription")
        except OrderNotApprovable:
            raise
        except Exception as e:
            if "is_gift" not in str(e) and "UndefinedColumn" not in type(e).__name__:
                log.warning("Gift renewal check failed for %s: %s", order.renew_email, e)
    # 60-day total limit — defense in depth (UI already blocks in renew_callback)
    if order.renew_email and order.duration_days:
        try:
            from packages import MAX_SUBSCRIPTION_DAYS

            pclient = await VPNPanelService.get_client(order.renew_email)
            if pclient is not None:
                inner = pclient.get("client") if isinstance(pclient.get("client"), dict) else pclient
                expiry_ms = 0
                if isinstance(inner, dict):
                    expiry_ms = int(inner.get("expiryTime") or 0)
                if not expiry_ms and isinstance(pclient, dict):
                    try:
                        expiry_ms = int(pclient.get("expiryTime") or 0)
                    except Exception:
                        expiry_ms = 0
                if expiry_ms:
                    now_ms = int(datetime.now(UTC).timestamp() * 1000)
                    remaining_ms = expiry_ms - now_ms
                    if remaining_ms > 0:
                        remaining_days = (remaining_ms + 86400000 - 1) // 86400000
                        if remaining_days + int(order.duration_days) > MAX_SUBSCRIPTION_DAYS:
                            raise RenewalLimitExceeded(
                                order_id,
                                f"renewal would exceed {MAX_SUBSCRIPTION_DAYS} days (remaining {remaining_days} + {order.duration_days})",
                            )
        except OrderNotApprovable:
            raise
        except VPNPanelError:
            raise
        except Exception as e:
            log.warning("Renewal 60-day pre-check failed for order #%s: %s", order_id, e)
    # Defense-in-depth IDOR: ensure renew_email belongs to the order's owner.
    # The UI check in renew_callback already blocks forged callbacks, but the
    # payment callback / free-confirm paths also reach here, so verify again.
    if order.renew_email:
        # Fail closed: if ownership cannot be confirmed, do NOT provision.
        # VPNPanelError (not OrderNotApprovable) keeps a paid order pending
        # for manual review instead of auto-refunding on a transient error.
        # NOTE: messages raised here must not contain "exceed"/"60" — the
        # payment callback treats those as over-limit renewals (refund path).
        try:
            try:
                result = await session.execute(select(User.telegram_id).where(User.id == order.user_id))
                owner_tg = result.scalar_one_or_none()
            except Exception as e:
                raise VPNPanelError(
                    "Renewal ownership check unavailable (database error) — refusing to provision renewal"
                ) from e
            if owner_tg is None:
                raise VPNPanelError(
                    "Renewal order has no resolvable owner user — refusing to provision renewal"
                )
            # Check DB history first
            db_owned = False
            try:
                hist = await session.execute(
                    select(Order.id)
                    .join(User, Order.user_id == User.id)
                    .where(Order.panel_email == order.renew_email, User.telegram_id == owner_tg)
                    .limit(1)
                )
                db_owned = hist.scalar_one_or_none() is not None
            except Exception:
                db_owned = False
            if not db_owned:
                # Fall back to panel tgId check
                try:
                    pclient = await VPNPanelService.get_client(order.renew_email)
                    if pclient is not None:
                        inner = pclient.get("client") if isinstance(pclient.get("client"), dict) else pclient
                        tg = None
                        if isinstance(inner, dict):
                            tg = inner.get("tgId")
                        if tg is None and isinstance(pclient, dict):
                            tg = pclient.get("tgId")
                        if tg is not None and str(tg) != str(owner_tg):
                            raise OrderNotApprovable(order_id, f"renew_email {order.renew_email} not owned by user {owner_tg}")
                        if tg is None:
                            # tg missing — verify via list
                            try:
                                clients = await VPNPanelService.get_clients_by_telegram_id(int(owner_tg))
                                if not any(c.get("email") == order.renew_email for c in clients):
                                    raise OrderNotApprovable(order_id, f"renew_email {order.renew_email} not owned by user {owner_tg}")
                            except VPNPanelError as e:
                                raise VPNPanelError(f"Cannot verify renewal ownership for {order.renew_email}: {e}") from e
                    else:
                        raise OrderNotApprovable(order_id, f"renew client {order.renew_email} not found")
                except OrderNotApprovable:
                    raise
                except Exception as e:
                    raise VPNPanelError(f"Ownership check failed for {order.renew_email}: {e}") from e
        except OrderNotApprovable:
            raise
        except VPNPanelError:
            raise
        except Exception as e:
            log.error(
                "Renewal ownership pre-check failed unexpectedly for order #%s — refusing to provision",
                order_id,
                exc_info=True,
            )
            raise VPNPanelError(
                "Renewal ownership could not be verified — refusing to provision renewal"
            ) from e

    claim = await session.execute(
        sa_update(Order)
        .where(Order.id == order_id, Order.status == "pending")
        .values(status="approved")
    )
    if claim.rowcount == 0:
        current = (
            await session.execute(select(Order.status).where(Order.id == order_id))
        ).scalar_one_or_none()
        if current == "approved":
            raise OrderAlreadyApproved(order_id)
        raise OrderNotApprovable(order_id, current)
    await session.commit()
    order.status = "approved"

    try:
        client = await VPNPanelService.extend_client(
            order.renew_email, order.duration_days, order.data_gb
        )
    except VPNPanelError:
        await session.rollback()
        await session.execute(
            sa_update(Order)
            .where(Order.id == order_id, Order.status == "approved")
            .values(status="pending")
        )
        await session.commit()
        raise
    except Exception:
        await session.rollback()
        try:
            await session.execute(
                sa_update(Order)
                .where(Order.id == order_id, Order.status == "approved")
                .values(status="pending")
            )
            await session.commit()
        except Exception:
            log.error("Failed to revert approved claim for order #%s", order_id, exc_info=True)
        raise VPNPanelError(
            f"Renewal failed for order #{order_id}: unexpected error"
        ) from None

    return {"email": client.get("email"), "sub_id": client.get("subId", ""), "links": []}


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
    # 15-min payment window: expired pending orders are not fulfillable
    if is_order_expired(order):
        # Mark as cancelled so callback's _paid_cancelled_flow can reverse if paid
        try:
            order.status = "cancelled"
            await session.commit()
        except Exception:
            pass
        raise OrderNotApprovable(order.id, "expired")
    outcome = await verify_payment(order.payment_authority, order.amount_toomans)
    if order.renew_email:
        await renew_order(session, order)
    else:
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
        current = (
            await session.execute(select(Order.status).where(Order.id == order_id))
        ).scalar_one_or_none()
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
            links = await VPNPanelService.get_client_links(
                email
            ) or await VPNPanelService.get_subscription_links(sub_id)
            if links:
                log.info("Order #%s: reusing existing panel client %s", order_id, email)
            else:
                # Client vanished from the panel — drop the stale reference.
                log.warning(
                    "Order #%s: panel client %s has no links; provisioning fresh.",
                    order_id,
                    email,
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
    except Exception:
        # Any other failure (DB, unexpected) must also revert the claim to avoid stuck approved without client.
        await session.rollback()
        try:
            await session.execute(
                sa_update(Order)
                .where(Order.id == order_id, Order.status == "approved")
                .values(status="pending")
            )
            await session.commit()
        except Exception:
            log.error("Failed to revert approved claim for order #%s", order_id, exc_info=True)
        raise VPNPanelError(
            f"Provisioning failed for order #{order_id}: unexpected error"
        ) from None

    return {"email": email, "sub_id": sub_id, "links": links}


def format_vpn_config(links: list[str]) -> str:
    """Format the config block (vless URIs) for a message."""
    return "\n".join(f"🔗 <code>{escape(link)}</code>" for link in links if link)
