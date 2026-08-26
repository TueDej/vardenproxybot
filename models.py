try:
    from datetime import UTC
except ImportError:  # Python <3.11
    from datetime import timezone

    UTC = timezone.utc  # type: ignore[no-redef]  # noqa: UP017
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="orders", lazy="raise")
