try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    orders: Mapped[list["Order"]] = relationship(
        back_populates="user",
        lazy="raise",  # async-safe: explicit selectinload where needed
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DiscountCode(Base):
    __tablename__ = "discount_codes"
    __table_args__ = (
        CheckConstraint("discount_percent >= 1 AND discount_percent <= 100", name="ck_discount_percent"),
        Index("ix_discount_codes_code", "code", unique=True),
        Index("ix_discount_codes_is_used", "is_used"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    is_used: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    used_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','cancelled')", name="ck_orders_status"
        ),
        CheckConstraint("duration_days >= 0", name="ck_orders_duration"),
        CheckConstraint("data_gb >= 0", name="ck_orders_data_gb"),
        CheckConstraint("amount_toomans >= 0", name="ck_orders_amount"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_payment_authority", "payment_authority", unique=False),
        Index("ix_orders_created_at", "created_at"),
        Index("ix_orders_sub_id", "sub_id"),
        Index("ix_orders_panel_email", "panel_email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_label: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    data_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_toomans: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, server_default="pending"
    )  # pending, approved, rejected, cancelled
    panel_email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sub_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When set, fulfilling this order extends the referenced panel client
    # (renewal) instead of creating a new one.
    renew_email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_authority: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_ref_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Discount code applied to this order (one-time use)
    discount_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    discount_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_amount_toomans: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("discount_codes.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="orders", lazy="raise")


class MessageLog(Base):
    __tablename__ = "message_logs"
    __table_args__ = (
        Index("ix_message_logs_created_at", "created_at"),
        Index("ix_message_logs_kind", "kind"),
        Index("ix_message_logs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    admin_user: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # custom / config_forward / broadcast
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )  # pending, sending, done, failed
    filter_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    text_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    panel_email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
