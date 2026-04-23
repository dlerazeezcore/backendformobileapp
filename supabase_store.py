from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    JSON,
    and_,
    Boolean,
    case,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    func,
    or_,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from phone_utils import normalize_phone, phone_lookup_candidates

APP_TIMEZONE = timezone(timedelta(hours=3), name="GMT+3")


def utcnow() -> datetime:
    return datetime.now(APP_TIMEZONE)


def parse_provider_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    iso_candidate = cleaned.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S%z",
        ):
            try:
                parsed = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(APP_TIMEZONE)


def parse_provider_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            try:
                return int(float(cleaned))
            except ValueError:
                return None
    return None


def _pick_first_provider_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        parsed = parse_provider_int(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _usage_unit_from_hint(unit_hint: str | None, *, total_raw: int | None, used_raw: int | None) -> str:
    normalized = str(unit_hint or "").strip().lower()
    if "byte" in normalized:
        return "bytes"
    if "kb" in normalized or "kib" in normalized:
        return "kb"
    if "gb" in normalized or "gib" in normalized:
        return "gb"
    if "mb" in normalized or "mib" in normalized:
        return "mb"

    candidates = [value for value in (total_raw, used_raw) if value is not None]
    if not candidates:
        return "mb"
    max_value = max(candidates)
    # Heuristic fallback for legacy provider payloads missing explicit unit:
    # very large values are usually bytes, medium-large values usually KB.
    if max_value >= 5_000_000:
        return "bytes"
    if max_value >= 5_000:
        return "kb"
    return "mb"


def _usage_value_to_mb(value: int | None, unit: str) -> int | None:
    if value is None:
        return None
    if value < 0:
        return 0
    if unit == "bytes":
        return max(int(round(value / (1024 * 1024))), 0)
    if unit == "kb":
        return max(int(round(value / 1024)), 0)
    if unit == "gb":
        return max(int(round(value * 1024)), 0)
    return value


def normalize_usage_pair_to_mb(
    *,
    total_raw: int | None,
    used_raw: int | None,
    unit_hint: str | None = None,
) -> tuple[int | None, int | None, str]:
    detected_unit = _usage_unit_from_hint(unit_hint, total_raw=total_raw, used_raw=used_raw)
    total_mb = _usage_value_to_mb(total_raw, detected_unit)
    used_mb = _usage_value_to_mb(used_raw, detected_unit)
    return total_mb, used_mb, detected_unit


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


class Base(DeclarativeBase):
    pass


class TimeMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AppUser(TimeMixin, Base):
    __tablename__ = "app_users"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    is_loyalty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_orders: Mapped[list["CustomerOrder"]] = relationship(back_populates="user")
    profiles: Mapped[list["ESimProfile"]] = relationship(back_populates="user")
    push_devices: Mapped[list["PushDevice"]] = relationship(back_populates="user", foreign_keys="PushDevice.user_id")


class AdminUser(TimeMixin, Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(64), default="admin", nullable=False, index=True)
    can_manage_users: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_manage_orders: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_manage_pricing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_manage_content: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_send_push: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(back_populates="admin_user")
    push_notifications: Mapped[list["PushNotification"]] = relationship(back_populates="sent_by_admin")
    push_devices: Mapped[list["PushDevice"]] = relationship(
        back_populates="admin_user",
        foreign_keys="PushDevice.admin_user_id",
    )


class PushDevice(TimeMixin, Base):
    __tablename__ = "push_devices"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND admin_user_id IS NULL) OR "
            "(user_id IS NULL AND admin_user_id IS NOT NULL) OR "
            "(user_id IS NULL AND admin_user_id IS NULL)",
            name="ck_push_devices_has_owner",
        ),
        Index("ix_push_devices_user_active", "user_id", "active"),
        Index("ix_push_devices_admin_active", "admin_user_id", "active"),
        Index("ix_push_devices_last_seen", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("app_users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    admin_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    token: Mapped[str] = mapped_column(String(512), unique=True)
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(255), index=True)
    app_version: Mapped[str | None] = mapped_column(String(64))
    locale: Mapped[str | None] = mapped_column(String(32))
    timezone_name: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    user: Mapped[AppUser | None] = relationship(back_populates="push_devices", foreign_keys=[user_id])
    admin_user: Mapped[AdminUser | None] = relationship(back_populates="push_devices", foreign_keys=[admin_user_id])


class PushNotification(TimeMixin, Base):
    __tablename__ = "push_notifications"
    __table_args__ = (
        Index("ix_push_notifications_status_created", "status", "created_at"),
        Index("ix_push_notifications_sender_created", "sent_by_admin_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    recipient_scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    target_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(64), default="firebase_fcm", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_tokens: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_by_admin_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        index=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_by_admin: Mapped[AdminUser | None] = relationship(back_populates="push_notifications")

class TelegramSupportMessage(TimeMixin, Base):
    __tablename__ = "telegram_support_messages"
    __table_args__ = (
        Index("ix_telegram_support_messages_user_created", "user_id", "created_at"),
        Index("ix_telegram_support_messages_direction_created", "direction", "created_at"),
        Index("ix_telegram_support_messages_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    admin_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    direction: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    push_delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

class ProviderFieldRule(TimeMixin, Base):
    __tablename__ = "provider_field_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, default="esim_access")
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    field_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ExchangeRate(TimeMixin, Base):
    __tablename__ = "exchange_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(8), index=True)
    quote_currency: Mapped[str] = mapped_column(String(8), index=True)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str | None] = mapped_column(String(120))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PricingRule(TimeMixin, Base):
    __tablename__ = "pricing_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_type: Mapped[str] = mapped_column(String(32), index=True, default="esim")
    rule_scope: Mapped[str] = mapped_column(String(32), index=True, default="global")
    country_code: Mapped[str | None] = mapped_column(String(8), index=True)
    package_code: Mapped[str | None] = mapped_column(String(120), index=True)
    provider_code: Mapped[str | None] = mapped_column(String(80), index=True)
    adjustment_type: Mapped[str] = mapped_column(String(16), default="percent", nullable=False)
    adjustment_value: Mapped[float] = mapped_column(Float, nullable=False)
    applies_to: Mapped[str] = mapped_column(String(32), default="provider_cost", nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(8))
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class DiscountRule(TimeMixin, Base):
    __tablename__ = "discount_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    service_type: Mapped[str] = mapped_column(String(32), index=True, default="esim")
    rule_scope: Mapped[str] = mapped_column(String(32), index=True, default="global")
    country_code: Mapped[str | None] = mapped_column(String(8), index=True)
    package_code: Mapped[str | None] = mapped_column(String(120), index=True)
    provider_code: Mapped[str | None] = mapped_column(String(80), index=True)
    discount_type: Mapped[str] = mapped_column(String(16), default="percent", nullable=False)
    discount_value: Mapped[float] = mapped_column(Float, nullable=False)
    applies_to: Mapped[str] = mapped_column(String(32), default="provider_cost", nullable=False)
    currency_code: Mapped[str | None] = mapped_column(String(8))
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class FeaturedLocation(TimeMixin, Base):
    __tablename__ = "featured_locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(255))
    service_type: Mapped[str] = mapped_column(String(32), index=True, default="esim")
    location_type: Mapped[str] = mapped_column(String(32), default="country")
    badge_text: Mapped[str | None] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_popular: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class CustomerOrder(TimeMixin, Base):
    __tablename__ = "customer_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("app_users.id", ondelete="SET NULL"), index=True)
    order_number: Mapped[str] = mapped_column(String(64), unique=True)
    order_status: Mapped[str] = mapped_column(String(80), default="BOOKED", nullable=False, index=True)
    currency_code: Mapped[str | None] = mapped_column(String(8))
    exchange_rate: Mapped[float | None] = mapped_column(Float)
    subtotal_minor: Mapped[int | None] = mapped_column(Integer)
    markup_minor: Mapped[int | None] = mapped_column(Integer)
    discount_minor: Mapped[int | None] = mapped_column(Integer, default=0)
    total_minor: Mapped[int | None] = mapped_column(Integer)
    refunded_minor: Mapped[int | None] = mapped_column(Integer, default=0)
    payment_method: Mapped[str | None] = mapped_column(String(32), index=True)
    payment_provider: Mapped[str | None] = mapped_column(String(64), index=True)
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped[AppUser | None] = relationship(back_populates="customer_orders")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="customer_order")
    lifecycle_events: Mapped[list["ESimLifecycleEvent"]] = relationship(back_populates="customer_order")
    payload_snapshots: Mapped[list["ProviderPayloadSnapshot"]] = relationship(back_populates="customer_order")
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(back_populates="customer_order")


class OrderItem(TimeMixin, Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_id: Mapped[int] = mapped_column(ForeignKey("customer_orders.id", ondelete="CASCADE"), index=True)
    service_type: Mapped[str] = mapped_column(String(32), default="esim", nullable=False, index=True)
    item_status: Mapped[str] = mapped_column(String(80), default="BOOKED", nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(80), default="esim_access", nullable=False)
    provider_order_no: Mapped[str | None] = mapped_column(String(120), unique=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_status: Mapped[str | None] = mapped_column(String(80))
    country_code: Mapped[str | None] = mapped_column(String(8), index=True)
    country_name: Mapped[str | None] = mapped_column(String(255))
    package_code: Mapped[str | None] = mapped_column(String(120), index=True)
    package_slug: Mapped[str | None] = mapped_column(String(120), index=True)
    package_name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider_price_minor: Mapped[int | None] = mapped_column(Integer)
    markup_minor: Mapped[int | None] = mapped_column(Integer)
    discount_minor: Mapped[int | None] = mapped_column(Integer)
    sale_price_minor: Mapped[int | None] = mapped_column(Integer)
    refund_amount_minor: Mapped[int | None] = mapped_column(Integer)
    payment_method: Mapped[str | None] = mapped_column(String(32), index=True)
    payment_provider: Mapped[str | None] = mapped_column(String(64), index=True)
    applied_pricing_rule_id: Mapped[int | None] = mapped_column(Integer)
    applied_discount_rule_id: Mapped[int | None] = mapped_column(Integer)
    applied_pricing_rule_type: Mapped[str | None] = mapped_column(String(16))
    applied_pricing_rule_value: Mapped[float | None] = mapped_column(Float)
    applied_pricing_rule_basis: Mapped[str | None] = mapped_column(String(32))
    applied_discount_rule_type: Mapped[str | None] = mapped_column(String(16))
    applied_discount_rule_value: Mapped[float | None] = mapped_column(Float)
    applied_discount_rule_basis: Mapped[str | None] = mapped_column(String(32))
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_provider_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    customer_order: Mapped[CustomerOrder] = relationship(back_populates="order_items")
    profiles: Mapped[list["ESimProfile"]] = relationship(back_populates="order_item")
    lifecycle_events: Mapped[list["ESimLifecycleEvent"]] = relationship(back_populates="order_item")
    payload_snapshots: Mapped[list["ProviderPayloadSnapshot"]] = relationship(back_populates="order_item")
    payment_attempts: Mapped[list["PaymentAttempt"]] = relationship(back_populates="order_item")


class PaymentAttempt(TimeMixin, Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_payment_attempts_transaction_id"),
        UniqueConstraint("provider", "provider_payment_id", name="uq_payment_attempts_provider_payment_id"),
        CheckConstraint(
            "(user_id IS NOT NULL) OR (admin_user_id IS NOT NULL)",
            name="ck_payment_attempts_has_owner",
        ),
        Index("ix_payment_attempts_customer_order_id", "customer_order_id"),
        Index("ix_payment_attempts_order_item_id", "order_item_id"),
        Index("ix_payment_attempts_admin_user_id", "admin_user_id"),
        Index("ix_payment_attempts_user_created", "user_id", "created_at"),
        Index("ix_payment_attempts_status_created", "status", "created_at"),
        Index("ix_payment_attempts_method_created", "payment_method", "created_at"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    customer_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    admin_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("admin_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    service_type: Mapped[str] = mapped_column(String(32), default="esim", nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_user_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    transaction_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    provider_request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provider_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_order: Mapped[CustomerOrder | None] = relationship(back_populates="payment_attempts")
    order_item: Mapped[OrderItem | None] = relationship(back_populates="payment_attempts")
    admin_user: Mapped[AdminUser | None] = relationship(back_populates="payment_attempts")
    provider_events: Mapped[list["PaymentProviderEvent"]] = relationship(back_populates="payment_attempt")


class PaymentProviderEvent(Base):
    __tablename__ = "payment_provider_events"
    __table_args__ = (
        Index("ix_payment_provider_events_provider_event_id", "provider", "provider_event_id"),
        Index("ix_payment_provider_events_attempt_id", "payment_attempt_id"),
        Index("ix_payment_provider_events_processed_created", "processed", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    payment_attempt_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("payment_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signature_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    payment_attempt: Mapped[PaymentAttempt | None] = relationship(back_populates="provider_events")


class ESimProfile(TimeMixin, Base):
    __tablename__ = "esim_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), ForeignKey("app_users.id", ondelete="SET NULL"), index=True)
    esim_tran_no: Mapped[str | None] = mapped_column(String(120), unique=True)
    iccid: Mapped[str | None] = mapped_column(String(120), unique=True)
    imsi: Mapped[str | None] = mapped_column(String(120))
    msisdn: Mapped[str | None] = mapped_column(String(120))
    activation_code: Mapped[str | None] = mapped_column(Text)
    qr_code_url: Mapped[str | None] = mapped_column(Text)
    install_url: Mapped[str | None] = mapped_column(Text)
    provider_status: Mapped[str | None] = mapped_column(String(80))
    app_status: Mapped[str | None] = mapped_column(String(80), index=True)
    installed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_type: Mapped[str | None] = mapped_column(String(80))
    total_data_mb: Mapped[int | None] = mapped_column(Integer)
    used_data_mb: Mapped[int | None] = mapped_column(Integer)
    remaining_data_mb: Mapped[int | None] = mapped_column(Integer)
    validity_days: Mapped[int | None] = mapped_column(Integer)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsuspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_provider_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    order_item: Mapped[OrderItem | None] = relationship(back_populates="profiles")
    user: Mapped[AppUser | None] = relationship(back_populates="profiles")
    lifecycle_events: Mapped[list["ESimLifecycleEvent"]] = relationship(back_populates="profile")
    payload_snapshots: Mapped[list["ProviderPayloadSnapshot"]] = relationship(back_populates="profile")


class ESimLifecycleEvent(TimeMixin, Base):
    __tablename__ = "esim_lifecycle_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_id: Mapped[int | None] = mapped_column(ForeignKey("customer_orders.id", ondelete="SET NULL"), index=True)
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("esim_profiles.id", ondelete="SET NULL"), index=True)
    service_type: Mapped[str | None] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str | None] = mapped_column(String(80))
    actor_type: Mapped[str | None] = mapped_column(String(32))
    actor_phone: Mapped[str | None] = mapped_column(String(64), index=True)
    platform_code: Mapped[str | None] = mapped_column(String(80))
    status_before: Mapped[str | None] = mapped_column(String(80))
    status_after: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    customer_order: Mapped[CustomerOrder | None] = relationship(back_populates="lifecycle_events")
    order_item: Mapped[OrderItem | None] = relationship(back_populates="lifecycle_events")
    profile: Mapped[ESimProfile | None] = relationship(back_populates="lifecycle_events")


class ProviderPayloadSnapshot(TimeMixin, Base):
    __tablename__ = "provider_payload_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, default="esim_access")
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    direction: Mapped[str] = mapped_column(String(32), default="response")
    customer_order_id: Mapped[int | None] = mapped_column(ForeignKey("customer_orders.id", ondelete="SET NULL"), index=True)
    order_item_id: Mapped[int | None] = mapped_column(ForeignKey("order_items.id", ondelete="SET NULL"), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("esim_profiles.id", ondelete="SET NULL"), index=True)
    selected_field_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    filtered_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    customer_order: Mapped[CustomerOrder | None] = relationship(back_populates="payload_snapshots")
    order_item: Mapped[OrderItem | None] = relationship(back_populates="payload_snapshots")
    profile: Mapped[ESimProfile | None] = relationship(back_populates="payload_snapshots")


def create_database(database_url: str) -> sessionmaker[Session]:
    database_url = normalize_database_url(database_url)
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    else:
        connect_args = {"options": "-c timezone=Asia/Baghdad"}
    engine = create_engine(database_url, future=True, connect_args=connect_args)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def extract_selected_fields(payload: dict[str, Any], field_paths: list[str]) -> dict[str, Any]:
    if not field_paths:
        return payload

    def merge_nested(target: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        for key, value in incoming.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                merge_nested(target[key], value)
            else:
                target[key] = value
        return target

    def extract_path(data: Any, tokens: list[str]) -> Any:
        if not tokens:
            return data
        token = tokens[0]
        is_array = token.endswith("[]")
        key = token[:-2] if is_array else token
        if not isinstance(data, dict) or key not in data:
            return None
        value = data[key]
        if is_array:
            if not isinstance(value, list):
                return None
            items = []
            for item in value:
                partial = extract_path(item, tokens[1:])
                if partial is not None:
                    items.append(partial)
            return {key: items}
        partial = extract_path(value, tokens[1:])
        if partial is None:
            return None
        return {key: partial}

    filtered: dict[str, Any] = {}
    for path in field_paths:
        tokens = [token for token in path.split(".") if token]
        partial = extract_path(payload, tokens)
        if partial is not None:
            merge_nested(filtered, partial)
    return filtered


class SupabaseStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _to_app_timezone(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=APP_TIMEZONE)
        return value.astimezone(APP_TIMEZONE)

    def _deactivate_previous_flagged_rows(
        self,
        *,
        model: Any,
        row: Any,
        flag_field: str,
        key_fields: list[str],
        new_start_field: str | None = None,
        previous_end_field: str | None = None,
    ) -> None:
        # Centralized policy: for admin configuration tables, one active/enabled row
        # should represent the current value for the same business key.
        if not bool(getattr(row, flag_field)):
            return
        filters = [getattr(model, key) == getattr(row, key) for key in key_fields]
        filters.extend(
            [
                getattr(model, flag_field).is_(True),
                getattr(model, "id") != row.id,
            ]
        )
        previous_rows = self.session.scalars(select(model).where(*filters)).all()
        new_start_at = (
            self._to_app_timezone(getattr(row, new_start_field))
            if new_start_field is not None
            else None
        )
        for previous in previous_rows:
            setattr(previous, flag_field, False)
            if previous_end_field is None or new_start_field is None or new_start_at is None:
                continue
            previous_end_at = self._to_app_timezone(getattr(previous, previous_end_field))
            if previous_end_at is None or previous_end_at > new_start_at:
                setattr(previous, previous_end_field, getattr(row, new_start_field))

    def _deactivate_matching_rows_without_insert(
        self,
        *,
        model: Any,
        flag_field: str,
        key_fields: list[str],
        payload: dict[str, Any],
        end_field: str | None = None,
        end_at: datetime | None = None,
    ) -> list[Any]:
        filters = [getattr(model, key) == payload.get(key) for key in key_fields]
        filters.append(getattr(model, flag_field).is_(True))
        rows = self.session.scalars(select(model).where(*filters)).all()
        if not rows:
            return []
        for row in rows:
            setattr(row, flag_field, False)
            if end_field is not None and end_at is not None:
                current_end = self._to_app_timezone(getattr(row, end_field))
                if current_end is None or current_end > end_at:
                    setattr(row, end_field, end_at)
        self.session.flush()
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return rows

    @staticmethod
    def _merge_json_dict(
        base: dict[str, Any] | None,
        incoming: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = dict(base or {})
        merged.update(incoming or {})
        return merged

    def ensure_user(
        self,
        *,
        phone: str,
        name: str,
        email: str | None = None,
        password_hash: str | None = None,
        status: str = "active",
        is_loyalty: bool = False,
        notes: str | None = None,
        blocked_at: datetime | None = None,
        deleted_at: datetime | None = None,
        last_login_at: datetime | None = None,
    ) -> AppUser:
        normalized_phone = normalize_phone(phone)
        user = self.session.scalar(select(AppUser).where(AppUser.phone.in_(phone_lookup_candidates(phone))))
        if user is None:
            user = AppUser(phone=normalized_phone, name=name)
            self.session.add(user)
        user.phone = normalized_phone
        user.name = name
        user.email = email
        if password_hash is not None:
            user.password_hash = password_hash
        user.status = status
        user.is_loyalty = is_loyalty
        user.notes = notes
        user.blocked_at = blocked_at
        user.deleted_at = deleted_at
        user.last_login_at = last_login_at
        self.session.flush()
        return user

    @staticmethod
    def _normalize_payment_status(status: str) -> str:
        normalized = status.strip().lower()
        if normalized == "cancelled":
            return "canceled"
        return normalized

    @staticmethod
    def _is_payment_terminal_status(status: str) -> bool:
        return status in {"paid", "failed", "canceled", "expired", "refunded"}

    def get_payment_attempt_by_id(
        self,
        payment_attempt_id: str,
        *,
        for_update: bool = False,
    ) -> PaymentAttempt | None:
        query = select(PaymentAttempt).where(PaymentAttempt.id == payment_attempt_id)
        if for_update:
            query = query.with_for_update()
        return self.session.scalar(query)

    def get_payment_attempt_by_transaction_id(
        self,
        transaction_id: str,
        *,
        for_update: bool = False,
    ) -> PaymentAttempt | None:
        query = select(PaymentAttempt).where(PaymentAttempt.transaction_id == transaction_id)
        if for_update:
            query = query.with_for_update()
        return self.session.scalar(query)

    def get_payment_attempt_by_provider_payment_id(
        self,
        *,
        provider: str | None,
        provider_payment_id: str,
        for_update: bool = False,
    ) -> PaymentAttempt | None:
        query = select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == provider_payment_id)
        if provider is not None:
            query = query.where(PaymentAttempt.provider == provider)
        if for_update:
            query = query.with_for_update()
        return self.session.scalar(query)

    def get_payment_attempt_by_any_reference(self, reference: str) -> PaymentAttempt | None:
        row = self.get_payment_attempt_by_id(reference)
        if row is not None:
            return row
        row = self.session.scalar(select(PaymentAttempt).where(PaymentAttempt.provider_payment_id == reference))
        if row is not None:
            return row
        row = self.session.scalar(select(PaymentAttempt).where(PaymentAttempt.transaction_id == reference))
        if row is not None:
            return row
        return None

    def create_payment_attempt(
        self,
        *,
        transaction_id: str,
        payment_method: str = "fib",
        provider: str | None = "fib",
        customer_order_id: int | None = None,
        provider_payment_id: str | None = None,
        provider_reference: str | None = None,
        external_user_ref: str | None = None,
        status: str = "pending",
        amount_minor: int = 0,
        currency_code: str = "IQD",
        user_id: str | None = None,
        admin_user_id: str | None = None,
        service_type: str = "esim",
        order_item_id: int | None = None,
        idempotency_key: str | None = None,
        failure_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_request: dict[str, Any] | None = None,
        provider_response: dict[str, Any] | None = None,
        paid_at: datetime | None = None,
        failed_at: datetime | None = None,
        canceled_at: datetime | None = None,
    ) -> PaymentAttempt:
        normalized_status = self._normalize_payment_status(status)
        row = PaymentAttempt(
            customer_order_id=customer_order_id,
            order_item_id=order_item_id,
            user_id=user_id,
            admin_user_id=admin_user_id,
            service_type=service_type,
            payment_method=payment_method.strip().lower(),
            provider=provider.strip().lower() if provider else None,
            status=normalized_status,
            amount_minor=amount_minor,
            currency_code=currency_code.strip().upper(),
            provider_payment_id=provider_payment_id,
            provider_reference=provider_reference,
            external_user_ref=external_user_ref,
            transaction_id=transaction_id,
            idempotency_key=idempotency_key,
            failure_reason=failure_reason,
            metadata_payload=metadata or {},
            provider_request=provider_request or {},
            provider_response=provider_response or {},
            paid_at=paid_at,
            failed_at=failed_at,
            canceled_at=canceled_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def update_payment_attempt(
        self,
        row: PaymentAttempt,
        *,
        status: str | None = None,
        customer_order_id: int | None = None,
        order_item_id: int | None = None,
        provider: str | None = None,
        provider_payment_id: str | None = None,
        provider_reference: str | None = None,
        external_user_ref: str | None = None,
        user_id: str | None = None,
        admin_user_id: str | None = None,
        idempotency_key: str | None = None,
        failure_reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_request: dict[str, Any] | None = None,
        provider_response: dict[str, Any] | None = None,
        merge_provider_response: bool = True,
        paid_at: datetime | None = None,
        failed_at: datetime | None = None,
        canceled_at: datetime | None = None,
    ) -> PaymentAttempt:
        if status is not None:
            row.status = self._normalize_payment_status(status)
        if customer_order_id is not None:
            row.customer_order_id = customer_order_id
        if order_item_id is not None:
            row.order_item_id = order_item_id
        if provider is not None:
            row.provider = provider.strip().lower() if provider else None
        if provider_payment_id and not row.provider_payment_id:
            row.provider_payment_id = provider_payment_id
        if provider_reference is not None:
            row.provider_reference = provider_reference
        if external_user_ref is not None:
            row.external_user_ref = external_user_ref
        if user_id is not None:
            row.user_id = user_id
        if admin_user_id is not None:
            row.admin_user_id = admin_user_id
        if idempotency_key is not None:
            row.idempotency_key = idempotency_key
        if failure_reason is not None:
            row.failure_reason = failure_reason
        if metadata is not None:
            row.metadata_payload = metadata
        if provider_request is not None:
            row.provider_request = provider_request
        if provider_response is not None:
            if merge_provider_response:
                row.provider_response = self._merge_json_dict(row.provider_response, provider_response)
            else:
                row.provider_response = provider_response
        if row.status == "paid":
            row.paid_at = paid_at or row.paid_at or utcnow()
        if row.status == "failed":
            row.failed_at = failed_at or row.failed_at or utcnow()
        if row.status == "canceled":
            row.canceled_at = canceled_at or row.canceled_at or utcnow()
        self.session.flush()
        return row

    def apply_payment_status_transition(
        self,
        row: PaymentAttempt,
        *,
        new_status: str,
        failure_reason: str | None = None,
        paid_at: datetime | None = None,
        failed_at: datetime | None = None,
        canceled_at: datetime | None = None,
    ) -> bool:
        normalized_new = self._normalize_payment_status(new_status)
        current = self._normalize_payment_status(row.status)
        if current == normalized_new:
            return False
        if self._is_payment_terminal_status(current):
            # Allow paid -> refunded only, otherwise keep terminal state.
            if not (current == "paid" and normalized_new == "refunded"):
                return False
        row.status = normalized_new
        if normalized_new == "paid":
            row.paid_at = paid_at or row.paid_at or utcnow()
        elif normalized_new == "failed":
            row.failed_at = failed_at or row.failed_at or utcnow()
            if failure_reason:
                row.failure_reason = failure_reason
        elif normalized_new == "canceled":
            row.canceled_at = canceled_at or row.canceled_at or utcnow()
        self.session.flush()
        return True

    def link_payment_attempt_to_order(
        self,
        *,
        payment_attempt: PaymentAttempt,
        customer_order: CustomerOrder,
        order_item: OrderItem,
    ) -> PaymentAttempt:
        payment_attempt.customer_order_id = customer_order.id
        payment_attempt.order_item_id = order_item.id
        payment_attempt.user_id = customer_order.user_id
        if customer_order.currency_code and not payment_attempt.currency_code:
            payment_attempt.currency_code = customer_order.currency_code
        self.session.flush()
        return payment_attempt

    def get_payment_provider_event(
        self,
        *,
        provider: str,
        provider_event_id: str,
    ) -> PaymentProviderEvent | None:
        return self.session.scalar(
            select(PaymentProviderEvent).where(
                PaymentProviderEvent.provider == provider,
                PaymentProviderEvent.provider_event_id == provider_event_id,
            )
        )

    def create_payment_provider_event(
        self,
        *,
        provider: str,
        event_type: str,
        raw_payload: dict[str, Any],
        payment_attempt_id: str | None = None,
        provider_event_id: str | None = None,
        signature_valid: bool | None = None,
        processed: bool = False,
        processing_error: str | None = None,
    ) -> PaymentProviderEvent:
        event = PaymentProviderEvent(
            payment_attempt_id=payment_attempt_id,
            provider=provider,
            event_type=event_type,
            provider_event_id=provider_event_id,
            signature_valid=signature_valid,
            raw_payload=raw_payload,
            processed=processed,
            processing_error=processing_error,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def mark_payment_provider_event_processed(
        self,
        event: PaymentProviderEvent,
        *,
        processed: bool,
        processing_error: str | None = None,
        payment_attempt_id: str | None = None,
    ) -> PaymentProviderEvent:
        event.processed = processed
        event.processing_error = processing_error
        if payment_attempt_id is not None:
            event.payment_attempt_id = payment_attempt_id
        self.session.flush()
        return event

    def upsert_push_device(
        self,
        *,
        user_id: str | None = None,
        admin_user_id: str | None = None,
        token: str,
        platform: str,
        device_id: str | None = None,
        app_version: str | None = None,
        locale: str | None = None,
        timezone_name: str | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> PushDevice:
        has_user_owner = bool(user_id)
        has_admin_owner = bool(admin_user_id)
        if has_user_owner and has_admin_owner:
            raise ValueError("Push device cannot be owned by both user and admin.")
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise ValueError("Push token is required.")
        normalized_platform = str(platform or "").strip().lower()
        if normalized_platform not in {"ios", "android", "web"}:
            raise ValueError("Push platform must be one of: ios, android, web.")

        row = self.session.scalar(select(PushDevice).where(PushDevice.token == normalized_token))
        if row is None:
            row = PushDevice(token=normalized_token, platform=normalized_platform)
            self.session.add(row)
        row.user_id = user_id
        row.admin_user_id = admin_user_id
        row.token = normalized_token
        row.platform = normalized_platform
        row.device_id = str(device_id or "").strip() or None
        row.app_version = str(app_version or "").strip() or None
        row.locale = str(locale or "").strip() or None
        row.timezone_name = str(timezone_name or "").strip() or None
        row.active = True
        row.last_seen_at = utcnow()
        row.custom_fields = custom_fields or {}
        self.session.flush()
        return row

    def deactivate_push_devices(
        self,
        *,
        user_id: str | None = None,
        admin_user_id: str | None = None,
        token: str | None = None,
        device_id: str | None = None,
    ) -> int:
        has_user_owner = bool(user_id)
        has_admin_owner = bool(admin_user_id)
        if not has_user_owner and not has_admin_owner:
            raise ValueError("Push device deactivation requires at least one subject owner.")
        if has_user_owner and has_admin_owner:
            raise ValueError("Push device deactivation requires exactly one subject owner.")
        filters = [PushDevice.active.is_(True)]
        if user_id is not None:
            filters.append(PushDevice.user_id == user_id)
        if admin_user_id is not None:
            filters.append(PushDevice.admin_user_id == admin_user_id)
        if token:
            filters.append(PushDevice.token == str(token).strip())
        if device_id:
            filters.append(PushDevice.device_id == str(device_id).strip())
        rows = self.session.scalars(select(PushDevice).where(*filters)).all()
        for row in rows:
            row.active = False
            row.last_seen_at = utcnow()
        self.session.flush()
        return len(rows)

    def deactivate_push_devices_public(
        self,
        *,
        token: str | None = None,
        device_id: str | None = None,
    ) -> int:
        if not token and not device_id:
            raise ValueError("Either token or deviceId is required")
        filters = [
            PushDevice.active.is_(True),
            PushDevice.user_id.is_(None),
            PushDevice.admin_user_id.is_(None),
        ]
        if token:
            filters.append(PushDevice.token == str(token).strip())
        if device_id:
            filters.append(PushDevice.device_id == str(device_id).strip())
        rows = self.session.scalars(select(PushDevice).where(*filters)).all()
        for row in rows:
            row.active = False
            row.last_seen_at = utcnow()
        self.session.flush()
        return len(rows)

    def deactivate_push_devices_by_tokens(self, tokens: list[str]) -> int:
        normalized = [str(item).strip() for item in tokens if str(item or "").strip()]
        if not normalized:
            return 0
        rows = self.session.scalars(select(PushDevice).where(PushDevice.token.in_(normalized))).all()
        for row in rows:
            row.active = False
            row.last_seen_at = utcnow()
        self.session.flush()
        return len(rows)

    def list_push_devices_for_user(
        self,
        *,
        user_id: str,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PushDevice]:
        effective_limit = max(1, min(limit, 500))
        effective_offset = max(0, offset)
        query = select(PushDevice).where(PushDevice.user_id == user_id)
        if active_only:
            query = query.where(PushDevice.active.is_(True))
        return self.session.scalars(
            query.order_by(PushDevice.updated_at.desc()).offset(effective_offset).limit(effective_limit)
        ).all()

    def list_push_tokens(
        self,
        *,
        user_ids: list[str] | None = None,
        active_only: bool = True,
        include_admin: bool = False,
        limit: int = 10000,
    ) -> list[str]:
        effective_limit = max(1, min(limit, 20000))
        query = select(PushDevice)
        if active_only:
            query = query.where(PushDevice.active.is_(True))
        if not include_admin:
            query = query.where(PushDevice.user_id.is_not(None))
        if user_ids:
            cleaned_user_ids = [str(item).strip() for item in user_ids if str(item).strip()]
            if cleaned_user_ids:
                query = query.where(PushDevice.user_id.in_(cleaned_user_ids))
        rows = self.session.scalars(query.limit(effective_limit)).all()
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for row in rows:
            token = row.token
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
        return unique_tokens

    def list_admin_push_tokens(
        self,
        *,
        active_only: bool = True,
        limit: int = 10000,
    ) -> list[str]:
        effective_limit = max(1, min(limit, 20000))
        query = select(PushDevice).where(PushDevice.admin_user_id.is_not(None))
        if active_only:
            query = query.where(PushDevice.active.is_(True))
        rows = self.session.scalars(query.limit(effective_limit)).all()
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for row in rows:
            token = row.token
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
        return unique_tokens

    def list_non_admin_push_tokens(
        self,
        *,
        active_only: bool = True,
        limit: int = 10000,
    ) -> list[str]:
        effective_limit = max(1, min(limit, 20000))
        query = select(PushDevice).where(PushDevice.admin_user_id.is_(None))
        if active_only:
            query = query.where(PushDevice.active.is_(True))
        rows = self.session.scalars(query.limit(effective_limit)).all()
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for row in rows:
            token = row.token
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
        return unique_tokens

    def list_all_push_tokens(
        self,
        *,
        active_only: bool = True,
        limit: int = 10000,
    ) -> list[str]:
        effective_limit = max(1, min(limit, 20000))
        query = select(PushDevice)
        if active_only:
            query = query.where(PushDevice.active.is_(True))
        rows = self.session.scalars(query.limit(effective_limit)).all()
        unique_tokens: list[str] = []
        seen: set[str] = set()
        for row in rows:
            token = row.token
            if token in seen:
                continue
            seen.add(token)
            unique_tokens.append(token)
        return unique_tokens

    def count_active_push_tokens(self, *, subject_type: str) -> int:
        normalized_subject_type = str(subject_type or "").strip().lower()
        query = select(func.count(PushDevice.id)).where(PushDevice.active.is_(True))
        if normalized_subject_type == "user":
            query = query.where(PushDevice.user_id.is_not(None))
        elif normalized_subject_type == "admin":
            query = query.where(PushDevice.admin_user_id.is_not(None))
        else:
            raise ValueError("subject_type must be either 'user' or 'admin'")
        return int(self.session.scalar(query) or 0)

    def count_push_tokens_by_platform(self, *, tokens: list[str]) -> dict[str, int]:
        normalized_tokens = [str(item).strip() for item in tokens if str(item or "").strip()]
        counts = {"ios": 0, "android": 0, "web": 0}
        if not normalized_tokens:
            return counts
        rows = self.session.scalars(select(PushDevice).where(PushDevice.token.in_(normalized_tokens))).all()
        seen: set[str] = set()
        for row in rows:
            token = str(row.token or "").strip()
            if not token or token in seen:
                continue
            seen.add(token)
            platform = str(row.platform or "").strip().lower()
            if platform in counts:
                counts[platform] += 1
        return counts

    def list_push_user_ids_for_audience(
        self,
        *,
        audience: str,
        limit: int = 20000,
    ) -> list[str]:
        normalized_audience = str(audience or "").strip().lower()
        effective_limit = max(1, min(limit, 50000))
        if normalized_audience not in {"authenticated", "loyalty", "active_esim"}:
            return []

        active_user_filters = [
            AppUser.status == "active",
            AppUser.deleted_at.is_(None),
            AppUser.blocked_at.is_(None),
        ]
        query = select(AppUser.id).where(*active_user_filters)
        if normalized_audience == "loyalty":
            query = query.where(AppUser.is_loyalty.is_(True))
        elif normalized_audience == "active_esim":
            active_profile_statuses = {"active", "installed", "suspended"}
            query = (
                query.join(ESimProfile, ESimProfile.user_id == AppUser.id)
                .where(
                    or_(
                        func.lower(ESimProfile.app_status).in_(active_profile_statuses),
                        and_(
                            ESimProfile.installed.is_(True),
                            ESimProfile.canceled_at.is_(None),
                            ESimProfile.refunded_at.is_(None),
                            ESimProfile.revoked_at.is_(None),
                        ),
                    ),
                    or_(ESimProfile.expires_at.is_(None), ESimProfile.expires_at > utcnow()),
                )
                .distinct()
            )
        user_ids = self.session.scalars(query.limit(effective_limit)).all()
        return [str(item) for item in user_ids if str(item).strip()]

    def list_push_tokens_for_audience(
        self,
        *,
        audience: str,
        limit: int = 10000,
    ) -> tuple[list[str], list[str]]:
        normalized_audience = str(audience or "").strip().lower()
        if normalized_audience == "all":
            return self.list_non_admin_push_tokens(active_only=True, limit=limit), []
        if normalized_audience == "admins":
            return self.list_admin_push_tokens(active_only=True, limit=limit), []
        if normalized_audience == "all_devices":
            return self.list_all_push_tokens(active_only=True, limit=limit), []
        user_ids = self.list_push_user_ids_for_audience(audience=normalized_audience, limit=limit * 2)
        tokens = self.list_push_tokens(user_ids=user_ids, active_only=True, limit=limit)
        return tokens, user_ids

    def get_push_notification_summary(self) -> dict[str, Any]:
        active_user_filters = [
            AppUser.status == "active",
            AppUser.deleted_at.is_(None),
            AppUser.blocked_at.is_(None),
        ]
        total_devices = int(
            self.session.scalar(select(func.count(PushDevice.id)))
            or 0
        )
        enabled_devices = int(
            self.session.scalar(select(func.count(PushDevice.id)).where(PushDevice.active.is_(True)))
            or 0
        )
        authenticated_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id))
                .select_from(PushDevice)
                .join(AppUser, PushDevice.user_id == AppUser.id)
                .where(PushDevice.active.is_(True), *active_user_filters)
            )
            or 0
        )
        loyalty_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id))
                .select_from(PushDevice)
                .join(AppUser, PushDevice.user_id == AppUser.id)
                .where(PushDevice.active.is_(True), *active_user_filters, AppUser.is_loyalty.is_(True))
            )
            or 0
        )
        active_profile_statuses = {"active", "installed", "suspended"}
        active_esim_devices = int(
            self.session.scalar(
                select(func.count(func.distinct(PushDevice.id)))
                .select_from(PushDevice)
                .join(AppUser, PushDevice.user_id == AppUser.id)
                .join(ESimProfile, ESimProfile.user_id == AppUser.id)
                .where(
                    PushDevice.active.is_(True),
                    *active_user_filters,
                    or_(
                        func.lower(ESimProfile.app_status).in_(active_profile_statuses),
                        and_(
                            ESimProfile.installed.is_(True),
                            ESimProfile.canceled_at.is_(None),
                            ESimProfile.refunded_at.is_(None),
                            ESimProfile.revoked_at.is_(None),
                        ),
                    ),
                    or_(ESimProfile.expires_at.is_(None), ESimProfile.expires_at > utcnow()),
                )
            )
            or 0
        )
        ios_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.platform == "ios",
                )
            )
            or 0
        )
        android_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.platform == "android",
                )
            )
            or 0
        )
        last_campaign = self.session.scalar(
            select(PushNotification).order_by(PushNotification.created_at.desc()).limit(1)
        )
        return {
            "totalDevices": total_devices,
            "enabledDevices": enabled_devices,
            "authenticatedDevices": authenticated_devices,
            "loyaltyDevices": loyalty_devices,
            "activeEsimDevices": active_esim_devices,
            "iosDevices": ios_devices,
            "androidDevices": android_devices,
            "lastCampaign": last_campaign,
        }

    @staticmethod
    def _token_prefix(token: str, *, size: int = 8) -> str:
        clean = str(token or "").strip()
        if not clean:
            return ""
        prefix = clean[:size]
        if len(clean) <= size:
            return prefix
        return f"{prefix}..."

    def get_push_devices_diagnostics(self, *, sample_limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(sample_limit, 50))
        total_push_devices = int(self.session.scalar(select(func.count(PushDevice.id))) or 0)
        active_push_devices = int(
            self.session.scalar(select(func.count(PushDevice.id)).where(PushDevice.active.is_(True))) or 0
        )
        active_push_devices_with_token = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.token.is_not(None),
                    PushDevice.token != "",
                )
            )
            or 0
        )
        active_ios_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.platform == "ios",
                )
            )
            or 0
        )
        active_android_devices = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.platform == "android",
                )
            )
            or 0
        )
        active_with_user_id = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.user_id.is_not(None),
                )
            )
            or 0
        )
        active_without_user_id = int(
            self.session.scalar(
                select(func.count(PushDevice.id)).where(
                    PushDevice.active.is_(True),
                    PushDevice.user_id.is_(None),
                )
            )
            or 0
        )
        latest_rows = self.session.scalars(
            select(PushDevice).order_by(PushDevice.updated_at.desc()).limit(limit)
        ).all()
        sample_latest_devices = [
            {
                "id": row.id,
                "platform": row.platform,
                "active": row.active,
                "tokenPrefix": self._token_prefix(row.token),
                "userId": row.user_id,
                "updatedAt": row.updated_at,
            }
            for row in latest_rows
        ]
        return {
            "totalPushDevices": total_push_devices,
            "activePushDevices": active_push_devices,
            "activePushDevicesWithToken": active_push_devices_with_token,
            "activePushDevicesByPlatform": {
                "ios": active_ios_devices,
                "android": active_android_devices,
            },
            "activePushDevicesWithUserId": active_with_user_id,
            "activePushDevicesWithoutUserId": active_without_user_id,
            "sampleLatestDevices": sample_latest_devices,
        }

    def create_push_notification(
        self,
        *,
        recipient_scope: str,
        title: str,
        body: str,
        provider: str,
        channel_id: str,
        image_url: str | None = None,
        sent_by_admin_id: str | None = None,
        target_user_ids: list[str] | None = None,
        data_payload: dict[str, Any] | None = None,
        provider_response: dict[str, Any] | None = None,
        status: str = "queued",
    ) -> PushNotification:
        row = PushNotification(
            recipient_scope=str(recipient_scope or "").strip() or "direct_tokens",
            title=str(title or "").strip(),
            body=str(body or "").strip(),
            provider=str(provider or "").strip() or "firebase_fcm",
            channel_id=str(channel_id or "").strip() or "general",
            image_url=str(image_url or "").strip() or None,
            sent_by_admin_id=sent_by_admin_id,
            target_user_ids=target_user_ids or [],
            data_payload=data_payload or {},
            provider_response=provider_response or {},
            status=str(status or "").strip() or "queued",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finalize_push_notification(
        self,
        *,
        row: PushNotification,
        status: str,
        success_count: int | None = None,
        failure_count: int | None = None,
        invalid_tokens: list[str] | None = None,
        provider_response: dict[str, Any] | None = None,
        error_message: str | None = None,
        sent_at: datetime | None = None,
    ) -> PushNotification:
        row.status = str(status or "").strip() or row.status
        if success_count is not None:
            row.success_count = max(int(success_count), 0)
        if failure_count is not None:
            row.failure_count = max(int(failure_count), 0)
        if invalid_tokens is not None:
            unique_invalid: list[str] = []
            seen: set[str] = set()
            for token in invalid_tokens:
                value = str(token or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                unique_invalid.append(value)
            row.invalid_tokens = unique_invalid
            row.invalid_token_count = len(unique_invalid)
        if provider_response is not None:
            row.provider_response = provider_response
        if error_message is not None:
            row.error_message = error_message
        if sent_at is not None:
            row.sent_at = sent_at
        self.session.flush()
        return row

    def list_push_notifications(self, *, limit: int = 100, offset: int = 0) -> list[PushNotification]:
        effective_limit = max(1, min(limit, 500))
        effective_offset = max(0, offset)
        return self.session.scalars(
            select(PushNotification).order_by(PushNotification.created_at.desc()).offset(effective_offset).limit(effective_limit)
        ).all()

    def ensure_admin_user(
        self,
        *,
        phone: str,
        name: str,
        email: str | None = None,
        password_hash: str | None = None,
        status: str = "active",
        role: str = "admin",
        can_manage_users: bool = False,
        can_manage_orders: bool = False,
        can_manage_pricing: bool = False,
        can_manage_content: bool = False,
        can_send_push: bool = False,
        notes: str | None = None,
        blocked_at: datetime | None = None,
        deleted_at: datetime | None = None,
        last_login_at: datetime | None = None,
        custom_fields: dict[str, Any] | None = None,
    ) -> AdminUser:
        normalized_phone = normalize_phone(phone)
        admin_user = self.session.scalar(select(AdminUser).where(AdminUser.phone.in_(phone_lookup_candidates(phone))))
        if admin_user is None:
            admin_user = AdminUser(phone=normalized_phone, name=name)
            self.session.add(admin_user)
        admin_user.phone = normalized_phone
        admin_user.name = name
        admin_user.email = email
        if password_hash is not None:
            admin_user.password_hash = password_hash
        admin_user.status = status
        admin_user.role = role
        admin_user.can_manage_users = can_manage_users
        admin_user.can_manage_orders = can_manage_orders
        admin_user.can_manage_pricing = can_manage_pricing
        admin_user.can_manage_content = can_manage_content
        admin_user.can_send_push = can_send_push
        admin_user.notes = notes
        admin_user.blocked_at = blocked_at
        admin_user.deleted_at = deleted_at
        admin_user.last_login_at = last_login_at
        admin_user.custom_fields = custom_fields or {}
        self.session.flush()
        return admin_user

    def save_field_rule(self, entity_type: str, field_paths: list[str], provider: str = "esim_access", enabled: bool = True) -> ProviderFieldRule:
        rule = self.session.scalar(
            select(ProviderFieldRule).where(
                ProviderFieldRule.provider == provider,
                ProviderFieldRule.entity_type == entity_type,
            )
        )
        if rule is None:
            rule = ProviderFieldRule(provider=provider, entity_type=entity_type)
            self.session.add(rule)
        rule.enabled = enabled
        rule.field_paths = field_paths
        self.session.commit()
        self.session.refresh(rule)
        return rule

    def build_order_number(self) -> str:
        return f"ORD-{utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"

    def get_active_exchange_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        at_time: datetime | None = None,
    ) -> ExchangeRate | None:
        if base_currency == quote_currency:
            return None
        now = at_time or utcnow()
        return self.session.scalar(
            select(ExchangeRate).where(
                ExchangeRate.base_currency == base_currency,
                ExchangeRate.quote_currency == quote_currency,
                ExchangeRate.active.is_(True),
                ExchangeRate.effective_at <= now,
                or_(ExchangeRate.expires_at.is_(None), ExchangeRate.expires_at > now),
            )
            .order_by(ExchangeRate.effective_at.desc(), ExchangeRate.id.desc())
            .limit(1)
        )

    def _rule_specificity(
        self,
        *,
        rule_scope: str,
        package_code: str | None,
        country_code: str | None,
        provider_code: str | None,
    ) -> int:
        if rule_scope == "package" and package_code:
            return 4
        if rule_scope == "country" and country_code:
            return 3
        if rule_scope == "provider" and provider_code:
            return 2
        if rule_scope == "global":
            return 1
        return 0

    def get_best_pricing_rule(
        self,
        *,
        service_type: str,
        package_code: str | None,
        country_code: str | None,
        provider_code: str | None,
        at_time: datetime | None = None,
    ) -> PricingRule | None:
        now = at_time or utcnow()
        scope_filters = [PricingRule.rule_scope == "global"]
        if package_code:
            scope_filters.append(and_(PricingRule.rule_scope == "package", PricingRule.package_code == package_code))
        if country_code:
            scope_filters.append(and_(PricingRule.rule_scope == "country", PricingRule.country_code == country_code))
        if provider_code:
            scope_filters.append(and_(PricingRule.rule_scope == "provider", PricingRule.provider_code == provider_code))
        specificity = case(
            (PricingRule.rule_scope == "package", 4),
            (PricingRule.rule_scope == "country", 3),
            (PricingRule.rule_scope == "provider", 2),
            else_=1,
        )
        return self.session.scalar(
            select(PricingRule).where(
                PricingRule.service_type == service_type,
                PricingRule.active.is_(True),
                or_(PricingRule.starts_at.is_(None), PricingRule.starts_at <= now),
                or_(PricingRule.ends_at.is_(None), PricingRule.ends_at > now),
                or_(*scope_filters),
            )
            .order_by(
                specificity.desc(),
                PricingRule.priority.desc(),
                PricingRule.created_at.desc(),
                PricingRule.id.desc(),
            )
            .limit(1)
        )

    def get_best_discount_rule(
        self,
        *,
        service_type: str,
        package_code: str | None,
        country_code: str | None,
        provider_code: str | None,
        at_time: datetime | None = None,
    ) -> DiscountRule | None:
        now = at_time or utcnow()
        scope_filters = [DiscountRule.rule_scope == "global"]
        if package_code:
            scope_filters.append(and_(DiscountRule.rule_scope == "package", DiscountRule.package_code == package_code))
        if country_code:
            scope_filters.append(and_(DiscountRule.rule_scope == "country", DiscountRule.country_code == country_code))
        if provider_code:
            scope_filters.append(and_(DiscountRule.rule_scope == "provider", DiscountRule.provider_code == provider_code))
        specificity = case(
            (DiscountRule.rule_scope == "package", 4),
            (DiscountRule.rule_scope == "country", 3),
            (DiscountRule.rule_scope == "provider", 2),
            else_=1,
        )
        return self.session.scalar(
            select(DiscountRule).where(
                DiscountRule.service_type == service_type,
                DiscountRule.active.is_(True),
                or_(DiscountRule.starts_at.is_(None), DiscountRule.starts_at <= now),
                or_(DiscountRule.ends_at.is_(None), DiscountRule.ends_at > now),
                or_(*scope_filters),
            )
            .order_by(
                specificity.desc(),
                DiscountRule.priority.desc(),
                DiscountRule.created_at.desc(),
                DiscountRule.id.desc(),
            )
            .limit(1)
        )

    def _calculate_adjustment_minor(
        self,
        *,
        adjustment_type: str,
        adjustment_value: float,
        basis_minor: int,
    ) -> int:
        if adjustment_type == "fixed":
            return max(int(round(adjustment_value)), 0)
        return max(int(round(basis_minor * (adjustment_value / 100.0))), 0)

    def save_managed_order(
        self,
        *,
        user_data: dict[str, Any],
        platform_code: str,
        platform_name: str | None,
        order_request: dict[str, Any],
        provider_response: dict[str, Any],
        currency_code: str | None = None,
        provider_currency_code: str | None = None,
        exchange_rate: float | None = None,
        sale_price_minor: int | None = None,
        provider_price_minor: int | None = None,
        country_code: str | None = None,
        country_name: str | None = None,
        package_code: str | None = None,
        package_slug: str | None = None,
        package_name: str | None = None,
        payment_method: str | None = None,
        payment_provider: str | None = None,
        custom_fields: dict[str, Any] | None = None,
        auto_commit: bool = True,
    ) -> tuple[CustomerOrder, OrderItem]:
        user = self.ensure_user(**user_data)
        _ = platform_name
        provider_obj = provider_response.get("obj") or {}
        package_info = (order_request.get("packageInfoList") or [{}])[0]
        item = self.session.scalar(
            select(OrderItem).where(
                OrderItem.provider_transaction_id == (
                    provider_obj.get("transactionId") or order_request.get("transactionId")
                )
            )
        )
        order = item.customer_order if item is not None else None
        if order is None:
            order = CustomerOrder(order_number=self.build_order_number())
            self.session.add(order)
        if item is None:
            item = OrderItem(service_type="esim")
            self.session.add(item)
        now = utcnow()
        source_provider_price_minor = provider_price_minor
        if source_provider_price_minor is None:
            source_provider_price_minor = parse_provider_int(package_info.get("price"))
        if source_provider_price_minor is None:
            source_provider_price_minor = 0
        source_currency_code = provider_currency_code or "USD"
        sale_currency_code = currency_code or source_currency_code
        applied_exchange_rate = exchange_rate
        if applied_exchange_rate is None:
            if source_currency_code == sale_currency_code:
                applied_exchange_rate = 1.0
            else:
                rate_row = self.get_active_exchange_rate(
                    base_currency=source_currency_code,
                    quote_currency=sale_currency_code,
                    at_time=now,
                )
                if rate_row is None:
                    raise ValueError(
                        f"No active exchange rate for {source_currency_code} -> {sale_currency_code}"
                    )
                applied_exchange_rate = rate_row.rate
        converted_provider_price_minor = int(round(source_provider_price_minor * applied_exchange_rate))
        pricing_rule = self.get_best_pricing_rule(
            service_type="esim",
            package_code=package_code or package_info.get("packageCode"),
            country_code=country_code,
            provider_code="esim_access",
            at_time=now,
        )
        pricing_basis_minor = converted_provider_price_minor
        markup_minor = 0
        if pricing_rule is not None:
            markup_minor = self._calculate_adjustment_minor(
                adjustment_type=pricing_rule.adjustment_type,
                adjustment_value=pricing_rule.adjustment_value,
                basis_minor=pricing_basis_minor,
            )
        subtotal_minor = converted_provider_price_minor
        total_before_discount_minor = subtotal_minor + markup_minor
        discount_rule = self.get_best_discount_rule(
            service_type="esim",
            package_code=package_code or package_info.get("packageCode"),
            country_code=country_code,
            provider_code="esim_access",
            at_time=now,
        )
        discount_basis_minor = subtotal_minor
        if discount_rule is not None and discount_rule.applies_to in {"item_total", "total_price", "booking_total"}:
            discount_basis_minor = total_before_discount_minor
        discount_minor = 0
        if discount_rule is not None:
            discount_minor = self._calculate_adjustment_minor(
                adjustment_type=discount_rule.discount_type,
                adjustment_value=discount_rule.discount_value,
                basis_minor=discount_basis_minor,
            )
        computed_sale_price_minor = max(total_before_discount_minor - discount_minor, 0)
        final_sale_price = (
            sale_price_minor
            if sale_price_minor is not None and pricing_rule is None and discount_rule is None
            else computed_sale_price_minor
        )
        order.user = user
        order.order_status = "BOOKED" if provider_response.get("success") else "FAILED"
        order.payment_method = str(payment_method or "").strip().lower() or None
        order.payment_provider = str(payment_provider or "").strip().lower() or None
        order.currency_code = sale_currency_code
        order.exchange_rate = applied_exchange_rate
        order.subtotal_minor = subtotal_minor
        order.markup_minor = markup_minor
        order.discount_minor = discount_minor
        order.total_minor = final_sale_price
        order.booked_at = now
        item.customer_order = order
        item.item_status = "BOOKED" if provider_response.get("success") else "FAILED"
        item.provider_order_no = provider_obj.get("orderNo")
        item.provider_transaction_id = provider_obj.get("transactionId") or order_request.get("transactionId")
        item.provider_status = "SUCCESS" if provider_response.get("success") else "FAILED"
        item.payment_method = str(payment_method or "").strip().lower() or None
        item.payment_provider = str(payment_provider or "").strip().lower() or None
        item.country_code = country_code
        item.country_name = country_name
        item.package_code = package_code or package_info.get("packageCode")
        item.package_slug = package_slug
        item.package_name = package_name
        quantity = parse_provider_int(package_info.get("count"))
        item.quantity = quantity if quantity and quantity > 0 else 1
        item.provider_price_minor = converted_provider_price_minor
        item.markup_minor = markup_minor
        item.discount_minor = discount_minor
        item.sale_price_minor = final_sale_price
        item.applied_pricing_rule_id = pricing_rule.id if pricing_rule else None
        item.applied_discount_rule_id = discount_rule.id if discount_rule else None
        item.applied_pricing_rule_type = pricing_rule.adjustment_type if pricing_rule else None
        item.applied_pricing_rule_value = pricing_rule.adjustment_value if pricing_rule else None
        item.applied_pricing_rule_basis = pricing_rule.applies_to if pricing_rule else None
        item.applied_discount_rule_type = discount_rule.discount_type if discount_rule else None
        item.applied_discount_rule_value = discount_rule.discount_value if discount_rule else None
        item.applied_discount_rule_basis = discount_rule.applies_to if discount_rule else None
        item.booked_at = now
        item.last_provider_sync_at = now
        item.custom_fields = custom_fields or {}
        self.session.flush()
        self.add_event(
            customer_order=order,
            order_item=item,
            profile=None,
            service_type="esim",
            event_type="BOOKED",
            source="internal_api",
            actor_type="user",
            actor_phone=user.phone,
            platform_code=platform_code,
            status_before=None,
            status_after="BOOKED",
            note="Managed order created",
            payload={
                "orderRequest": order_request,
                "providerResponse": provider_response,
                "pricingSnapshot": {
                    "providerPriceMinor": converted_provider_price_minor,
                    "currencyCode": sale_currency_code,
                    "exchangeRate": applied_exchange_rate,
                    "markupMinor": markup_minor,
                    "discountMinor": discount_minor,
                    "salePriceMinor": final_sale_price,
                    "pricingRuleId": pricing_rule.id if pricing_rule else None,
                    "discountRuleId": discount_rule.id if discount_rule else None,
                },
            },
        )
        self.save_payload("order_request", "request", order_request, customer_order=order, order_item=item)
        self.save_payload("order_response", "response", provider_response, customer_order=order, order_item=item)
        if auto_commit:
            self.session.commit()
            self.session.refresh(order)
            self.session.refresh(item)
        else:
            self.session.flush()
        return order, item

    def sync_profiles(
        self,
        provider_response: dict[str, Any],
        *,
        platform_code: str | None = None,
        platform_name: str | None = None,
        actor_phone: str | None = None,
    ) -> list[ESimProfile]:
        result: list[ESimProfile] = []
        for item in ((provider_response.get("obj") or {}).get("esimList") or []):
            order_item = None
            if item.get("orderNo"):
                order_item = self.session.scalar(select(OrderItem).where(OrderItem.provider_order_no == item["orderNo"]))
            if order_item is None:
                customer_order = CustomerOrder(
                    order_number=self.build_order_number(),
                    order_status=item.get("esimStatus") or "PENDING",
                    booked_at=utcnow(),
                )
                self.session.add(customer_order)
                self.session.flush()
                order_item = OrderItem(
                    customer_order=customer_order,
                    service_type="esim",
                    item_status=item.get("esimStatus") or "PENDING",
                    provider="esim_access",
                    provider_order_no=item.get("orderNo"),
                    provider_transaction_id=item.get("transactionId"),
                    provider_status=item.get("smdpStatus"),
                    booked_at=utcnow(),
                )
                self.session.add(order_item)
                self.session.flush()
            profile = None
            if item.get("iccid"):
                profile = self.session.scalar(select(ESimProfile).where(ESimProfile.iccid == item["iccid"]))
            if profile is None and item.get("esimTranNo"):
                profile = self.session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == item["esimTranNo"]))
            if profile is None:
                profile = ESimProfile()
                self.session.add(profile)
            before = profile.app_status
            raw_total = _pick_first_provider_int(
                item,
                (
                    "totalDataMb",
                    "totalVolume",
                    "totalData",
                    "totalDataKb",
                    "totalDataBytes",
                ),
            )
            raw_used = _pick_first_provider_int(
                item,
                (
                    "usedDataMb",
                    "orderUsage",
                    "dataUsage",
                    "usedDataKb",
                    "usedDataBytes",
                    "dataUsageBytes",
                ),
            )
            total_data_mb, used_data_mb, _detected_unit = normalize_usage_pair_to_mb(
                total_raw=raw_total,
                used_raw=raw_used,
                unit_hint=str(
                    item.get("usageUnit")
                    or item.get("dataUnit")
                    or item.get("volumeUnit")
                    or item.get("unit")
                    or ""
                ),
            )
            remaining_data_mb = None
            if total_data_mb is not None and used_data_mb is not None:
                remaining_data_mb = max(total_data_mb - used_data_mb, 0)
            validity_days = parse_provider_int(item.get("totalDuration"))
            status_after = item.get("esimStatus")
            expires_at = parse_provider_datetime(item.get("expiredTime"))
            profile.order_item = order_item
            profile.user = order_item.customer_order.user
            profile.esim_tran_no = item.get("esimTranNo")
            profile.iccid = item.get("iccid")
            profile.imsi = item.get("imsi")
            profile.msisdn = item.get("msisdn")
            profile.activation_code = item.get("ac")
            profile.qr_code_url = item.get("qrCodeUrl")
            profile.install_url = item.get("shortUrl")
            profile.provider_status = item.get("smdpStatus")
            profile.app_status = status_after
            profile.data_type = None if item.get("dataType") is None else str(item.get("dataType"))
            profile.total_data_mb = total_data_mb
            profile.used_data_mb = used_data_mb
            profile.remaining_data_mb = remaining_data_mb
            profile.validity_days = validity_days
            if used_data_mb not in (None, 0) and profile.activated_at is None:
                profile.activated_at = utcnow()
            if expires_at is not None:
                profile.expires_at = expires_at
            elif profile.activated_at is not None and validity_days and profile.expires_at is None:
                profile.expires_at = profile.activated_at + timedelta(days=validity_days)
            profile.last_provider_sync_at = utcnow()
            profile.custom_fields = self._merge_json_dict(
                profile.custom_fields,
                {
                    "usageUnit": "MB",
                    "packageDataMb": total_data_mb,
                },
            )
            order_item.item_status = profile.app_status or order_item.item_status
            order_item.provider_status = profile.provider_status
            order_item.last_provider_sync_at = utcnow()
            order_item.customer_order.order_status = order_item.item_status or order_item.customer_order.order_status
            self.add_event(
                customer_order=order_item.customer_order,
                order_item=order_item,
                profile=profile,
                service_type="esim",
                event_type="PROVIDER_SYNC",
                source="provider_query",
                actor_type="system",
                actor_phone=actor_phone,
                platform_code=platform_code,
                status_before=before,
                status_after=profile.app_status,
                note="Profile synced from provider query",
                payload=item,
            )
            self.save_payload(
                "profile_query_response",
                "response",
                item,
                customer_order=order_item.customer_order,
                order_item=order_item,
                profile=profile,
            )
            result.append(profile)
        self.session.commit()
        return result

    def sync_usage_records(
        self,
        provider_response: dict[str, Any],
        *,
        actor_phone: str | None = None,
    ) -> list[ESimProfile]:
        result: list[ESimProfile] = []
        usage_records = ((provider_response.get("obj") or {}).get("esimUsageList") or [])
        for record in usage_records:
            esim_tran_no = record.get("esimTranNo")
            if not esim_tran_no:
                continue
            profile = self.session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == esim_tran_no))
            if profile is None:
                continue
            before = profile.used_data_mb
            raw_total = _pick_first_provider_int(
                record,
                (
                    "totalDataMb",
                    "totalData",
                    "totalDataKb",
                    "totalDataBytes",
                ),
            )
            raw_used = _pick_first_provider_int(
                record,
                (
                    "usedDataMb",
                    "dataUsage",
                    "usedDataKb",
                    "usedDataBytes",
                    "dataUsageBytes",
                ),
            )
            total_data_mb, used_data_mb, _detected_unit = normalize_usage_pair_to_mb(
                total_raw=raw_total,
                used_raw=raw_used,
                unit_hint=str(
                    record.get("usageUnit")
                    or record.get("dataUnit")
                    or record.get("volumeUnit")
                    or record.get("unit")
                    or ""
                ),
            )
            remaining_data_mb = None
            if total_data_mb is not None and used_data_mb is not None:
                remaining_data_mb = max(total_data_mb - used_data_mb, 0)
            profile.total_data_mb = total_data_mb if total_data_mb is not None else profile.total_data_mb
            profile.used_data_mb = used_data_mb
            profile.remaining_data_mb = remaining_data_mb
            profile.custom_fields = self._merge_json_dict(
                profile.custom_fields,
                {
                    "usageUnit": "MB",
                    "packageDataMb": profile.total_data_mb,
                },
            )
            profile.last_provider_sync_at = parse_provider_datetime(record.get("lastUpdateTime")) or utcnow()
            if used_data_mb not in (None, 0) and profile.activated_at is None:
                profile.activated_at = utcnow()
                profile.app_status = profile.app_status or "ACTIVE"
            if profile.activated_at is not None and profile.validity_days and profile.expires_at is None:
                profile.expires_at = profile.activated_at + timedelta(days=profile.validity_days)
            order_item = profile.order_item
            customer_order = order_item.customer_order if order_item is not None else None
            if order_item is not None and profile.app_status:
                order_item.item_status = profile.app_status
                order_item.last_provider_sync_at = profile.last_provider_sync_at
            if customer_order is not None and order_item is not None and order_item.item_status:
                customer_order.order_status = order_item.item_status
            self.add_event(
                customer_order=customer_order,
                order_item=order_item,
                profile=profile,
                service_type="esim",
                event_type="PROVIDER_USAGE_SYNC",
                source="provider_usage_query",
                actor_type="system",
                actor_phone=actor_phone,
                platform_code=None,
                status_before=None if before is None else str(before),
                status_after=None if used_data_mb is None else str(used_data_mb),
                note="Profile usage synced from provider usage query",
                payload=record,
            )
            self.save_payload(
                "usage_query_response",
                "response",
                record,
                customer_order=customer_order,
                order_item=order_item,
                profile=profile,
            )
            result.append(profile)
        self.session.commit()
        return result

    def apply_profile_action(
        self,
        *,
        action: str,
        identifier_key: str,
        identifier_value: str,
        platform_code: str | None = None,
        actor_phone: str | None = None,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
        refund_amount_minor: int | None = None,
    ) -> ESimProfile | None:
        if identifier_key == "iccid":
            profile = self.session.scalar(select(ESimProfile).where(ESimProfile.iccid == identifier_value))
        else:
            profile = self.session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == identifier_value))
        if profile is None:
            self.session.commit()
            return None
        order_item = profile.order_item
        customer_order = order_item.customer_order if order_item is not None else None
        before = profile.app_status
        now = utcnow()
        if action == "install":
            profile.installed = True
            profile.installed_at = now
        elif action == "activate":
            profile.installed = True
            profile.installed_at = profile.installed_at or now
            profile.activated_at = profile.activated_at or now
            profile.app_status = "ACTIVE"
            if profile.validity_days and profile.expires_at is None:
                profile.expires_at = profile.activated_at + timedelta(days=profile.validity_days)
            if order_item:
                order_item.item_status = "ACTIVE"
            if customer_order:
                customer_order.order_status = "ACTIVE"
        elif action == "cancel":
            profile.app_status = "CANCELLED"
            profile.canceled_at = now
            if order_item:
                order_item.item_status = "CANCELLED"
                order_item.canceled_at = now
            if customer_order:
                customer_order.order_status = "CANCELLED"
        elif action == "revoke":
            profile.app_status = "REVOKED"
            profile.revoked_at = now
            if order_item:
                order_item.item_status = "REVOKED"
                order_item.revoked_at = now
            if customer_order:
                customer_order.order_status = "REVOKED"
        elif action == "suspend":
            profile.app_status = "SUSPENDED"
            profile.suspended_at = now
            if order_item:
                order_item.item_status = "SUSPENDED"
            if customer_order:
                customer_order.order_status = "SUSPENDED"
        elif action == "unsuspend":
            profile.app_status = "ACTIVE"
            profile.unsuspended_at = now
            if order_item:
                order_item.item_status = "ACTIVE"
            if customer_order:
                customer_order.order_status = "ACTIVE"
        elif action == "refund":
            profile.app_status = "REFUNDED"
            profile.refunded_at = now
            if order_item:
                order_item.item_status = "REFUNDED"
                order_item.refunded_at = now
                order_item.refund_amount_minor = refund_amount_minor
            if customer_order:
                customer_order.order_status = "REFUNDED"
                customer_order.refunded_minor = refund_amount_minor
        self.add_event(
            customer_order=customer_order,
            order_item=order_item,
            profile=profile,
            service_type="esim",
            event_type=action.upper(),
            source="internal_api",
            actor_type="user",
            actor_phone=actor_phone,
            platform_code=platform_code,
            status_before=before,
            status_after=profile.app_status,
            note=note,
            payload=payload or {},
        )
        self.save_payload(
            f"{action}_response",
            "response",
            payload or {},
            customer_order=customer_order,
            order_item=order_item,
            profile=profile,
        )
        self.session.commit()
        self.session.refresh(profile)
        return profile

    def record_webhook(self, payload: dict[str, Any]) -> ESimLifecycleEvent:
        content = payload.get("content") or {}
        profile = None
        order_item = None
        if content.get("iccid"):
            profile = self.session.scalar(select(ESimProfile).where(ESimProfile.iccid == content["iccid"]))
        if profile is None and content.get("esimTranNo"):
            profile = self.session.scalar(select(ESimProfile).where(ESimProfile.esim_tran_no == content["esimTranNo"]))
        if content.get("orderNo"):
            order_item = self.session.scalar(select(OrderItem).where(OrderItem.provider_order_no == content["orderNo"]))
        if order_item is None and profile is not None:
            order_item = profile.order_item
        customer_order = order_item.customer_order if order_item is not None else None
        if profile is not None and content.get("esimStatus"):
            profile.app_status = content["esimStatus"]
            profile.provider_status = content.get("smdpStatus")
            profile.expires_at = parse_provider_datetime(content.get("expiredTime")) or profile.expires_at
            profile.last_provider_sync_at = utcnow()
        if order_item is not None and (content.get("esimStatus") or content.get("orderStatus")):
            order_item.item_status = content.get("esimStatus") or content.get("orderStatus")
            order_item.provider_status = content.get("smdpStatus")
            order_item.last_provider_sync_at = utcnow()
        if customer_order is not None and (content.get("esimStatus") or content.get("orderStatus")):
            customer_order.order_status = content.get("esimStatus") or content.get("orderStatus")
        event = self.add_event(
            customer_order=customer_order,
            order_item=order_item,
            profile=profile,
            service_type="esim",
            event_type=payload.get("notifyType", "WEBHOOK"),
            source="provider_webhook",
            actor_type="provider",
            actor_phone=None,
            platform_code="provider_webhook",
            status_before=None,
            status_after=content.get("esimStatus") or content.get("orderStatus"),
            note="Webhook received from eSIM Access",
            payload=payload,
        )
        self.save_payload("webhook_event", "response", payload, customer_order=customer_order, order_item=order_item, profile=profile)
        self.session.commit()
        self.session.refresh(event)
        return event

    def save_pricing_rule(self, payload: dict[str, Any]) -> PricingRule:
        payload = dict(payload)
        if payload.get("active", True) is False:
            ended_at = self._to_app_timezone(payload.get("starts_at")) or utcnow()
            updated_rows = self._deactivate_matching_rows_without_insert(
                model=PricingRule,
                flag_field="active",
                key_fields=[
                    "service_type",
                    "rule_scope",
                    "country_code",
                    "package_code",
                    "provider_code",
                    "applies_to",
                    "currency_code",
                ],
                payload=payload,
                end_field="ends_at",
                end_at=ended_at,
            )
            if updated_rows:
                self.session.commit()
                self.session.refresh(updated_rows[0])
                return updated_rows[0]
        if payload.get("active", True) and payload.get("starts_at") is None:
            payload["starts_at"] = utcnow()
        row = PricingRule(**payload)
        self.session.add(row)
        self.session.flush()
        self._deactivate_previous_flagged_rows(
            model=PricingRule,
            row=row,
            flag_field="active",
            key_fields=[
                "service_type",
                "rule_scope",
                "country_code",
                "package_code",
                "provider_code",
                "applies_to",
                "currency_code",
            ],
            new_start_field="starts_at",
            previous_end_field="ends_at",
        )
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_discount_rule(self, payload: dict[str, Any]) -> DiscountRule:
        payload = dict(payload)
        if payload.get("active", True) is False:
            ended_at = self._to_app_timezone(payload.get("starts_at")) or utcnow()
            updated_rows = self._deactivate_matching_rows_without_insert(
                model=DiscountRule,
                flag_field="active",
                key_fields=[
                    "service_type",
                    "rule_scope",
                    "country_code",
                    "package_code",
                    "provider_code",
                    "applies_to",
                    "currency_code",
                ],
                payload=payload,
                end_field="ends_at",
                end_at=ended_at,
            )
            if updated_rows:
                self.session.commit()
                self.session.refresh(updated_rows[0])
                return updated_rows[0]
        if payload.get("active", True) and payload.get("starts_at") is None:
            payload["starts_at"] = utcnow()
        row = DiscountRule(**payload)
        self.session.add(row)
        self.session.flush()
        self._deactivate_previous_flagged_rows(
            model=DiscountRule,
            row=row,
            flag_field="active",
            key_fields=[
                "service_type",
                "rule_scope",
                "country_code",
                "package_code",
                "provider_code",
                "applies_to",
                "currency_code",
            ],
            new_start_field="starts_at",
            previous_end_field="ends_at",
        )
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_featured_location(self, payload: dict[str, Any]) -> FeaturedLocation:
        payload = dict(payload)
        if payload.get("enabled", True) is False:
            ended_at = self._to_app_timezone(payload.get("starts_at")) or utcnow()
            updated_rows = self._deactivate_matching_rows_without_insert(
                model=FeaturedLocation,
                flag_field="enabled",
                key_fields=["service_type", "location_type", "code"],
                payload=payload,
                end_field="ends_at",
                end_at=ended_at,
            )
            if updated_rows:
                self.session.commit()
                self.session.refresh(updated_rows[0])
                return updated_rows[0]
        if payload.get("enabled", True) and payload.get("starts_at") is None:
            payload["starts_at"] = utcnow()
        row = FeaturedLocation(**payload)
        self.session.add(row)
        self.session.flush()
        self._deactivate_previous_flagged_rows(
            model=FeaturedLocation,
            row=row,
            flag_field="enabled",
            key_fields=["service_type", "location_type", "code"],
            new_start_field="starts_at",
            previous_end_field="ends_at",
        )
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_exchange_rate(self, payload: dict[str, Any]) -> ExchangeRate:
        payload = dict(payload)
        if payload.get("active", True) is False:
            ended_at = self._to_app_timezone(payload.get("effective_at")) or utcnow()
            updated_rows = self._deactivate_matching_rows_without_insert(
                model=ExchangeRate,
                flag_field="active",
                key_fields=["base_currency", "quote_currency"],
                payload=payload,
                end_field="expires_at",
                end_at=ended_at,
            )
            if updated_rows:
                self.session.commit()
                self.session.refresh(updated_rows[0])
                return updated_rows[0]
        if payload.get("effective_at") is None:
            payload["effective_at"] = utcnow()
        row = ExchangeRate(**payload)
        self.session.add(row)
        self.session.flush()
        self._deactivate_previous_flagged_rows(
            model=ExchangeRate,
            row=row,
            flag_field="active",
            key_fields=["base_currency", "quote_currency"],
            new_start_field="effective_at",
            previous_end_field="expires_at",
        )
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_rows(
        self,
        model: Any,
        *,
        exclude: set[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        effective_limit = max(1, min(limit, 500))
        effective_offset = max(0, offset)
        rows = self.session.scalars(
            select(model)
            .order_by(model.created_at.desc())
            .offset(effective_offset)
            .limit(effective_limit)
        ).all()
        exclude = exclude or set()
        result = []
        for row in rows:
            result.append(
                {
                    column.name: (
                        self._to_app_timezone(getattr(row, column.name))
                        if isinstance(getattr(row, column.name), datetime)
                        else getattr(row, column.name)
                    )
                    for column in row.__table__.columns
                    if column.name not in exclude
                }
            )
        return result

    def list_public_featured_locations(self, *, service_type: str = "esim") -> list[dict[str, Any]]:
        now = utcnow()
        rows = self.session.scalars(
            select(FeaturedLocation)
            .where(
                FeaturedLocation.service_type == service_type,
                FeaturedLocation.enabled.is_(True),
                FeaturedLocation.is_popular.is_(True),
                or_(FeaturedLocation.starts_at.is_(None), FeaturedLocation.starts_at <= now),
                or_(FeaturedLocation.ends_at.is_(None), FeaturedLocation.ends_at > now),
            )
            .order_by(
                FeaturedLocation.updated_at.desc(),
                FeaturedLocation.created_at.desc(),
                FeaturedLocation.id.desc(),
            )
        ).all()

        latest_by_code: dict[str, FeaturedLocation] = {}
        for row in rows:
            key = str(row.code or "").strip().upper()
            if not key:
                continue
            if key not in latest_by_code:
                latest_by_code[key] = row

        deduped_rows = list(latest_by_code.values())
        deduped_rows.sort(
            key=lambda row: (
                int(row.sort_order or 0),
                str(row.name or ""),
                str(row.code or ""),
            )
        )

        result: list[dict[str, Any]] = []
        for row in deduped_rows:
            result.append(
                {
                    "code": row.code,
                    "name": row.name,
                    "serviceType": row.service_type,
                    "locationType": row.location_type,
                    "isPopular": bool(row.is_popular),
                    "enabled": bool(row.enabled),
                    "sortOrder": int(row.sort_order or 0),
                    "updatedAt": self._to_app_timezone(row.updated_at),
                }
            )
        return result

    def get_current_exchange_rate_settings(self) -> ExchangeRate | None:
        return self.get_active_exchange_rate(base_currency="USD", quote_currency="IQD")

    def list_profiles_for_user(
        self,
        *,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        installed: bool | None = None,
    ) -> tuple[list[ESimProfile], int]:
        effective_limit = max(1, min(limit, 500))
        effective_offset = max(0, offset)

        query = select(ESimProfile).where(ESimProfile.user_id == user_id)
        if status is not None and status.strip():
            normalized_status = status.strip().upper()
            query = query.where(func.upper(func.coalesce(ESimProfile.app_status, "")) == normalized_status)
        if installed is not None:
            query = query.where(ESimProfile.installed.is_(installed))

        count_query = select(func.count()).select_from(query.subquery())
        total = int(self.session.scalar(count_query) or 0)

        rows = self.session.scalars(
            query
            .order_by(ESimProfile.updated_at.desc(), ESimProfile.id.desc())
            .offset(effective_offset)
            .limit(effective_limit)
        ).all()
        return rows, total

    def save_payload(
        self,
        entity_type: str,
        direction: str,
        payload: dict[str, Any],
        *,
        customer_order: CustomerOrder | None = None,
        order_item: OrderItem | None = None,
        profile: ESimProfile | None = None,
    ) -> ProviderPayloadSnapshot:
        rule = self.session.scalar(
            select(ProviderFieldRule).where(
                ProviderFieldRule.provider == "esim_access",
                ProviderFieldRule.entity_type == entity_type,
                ProviderFieldRule.enabled.is_(True),
            )
        )
        field_paths = rule.field_paths if rule else []
        filtered_payload = extract_selected_fields(payload, field_paths)
        snapshot = ProviderPayloadSnapshot(
            provider="esim_access",
            entity_type=entity_type,
            direction=direction,
            customer_order=customer_order,
            order_item=order_item,
            profile=profile,
            selected_field_paths=field_paths,
            filtered_payload=filtered_payload,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def add_event(
        self,
        *,
        customer_order: CustomerOrder | None,
        order_item: OrderItem | None,
        profile: ESimProfile | None,
        service_type: str | None,
        event_type: str,
        source: str | None,
        actor_type: str | None,
        actor_phone: str | None,
        platform_code: str | None,
        status_before: str | None,
        status_after: str | None,
        note: str | None,
        payload: dict[str, Any],
    ) -> ESimLifecycleEvent:
        event = ESimLifecycleEvent(
            customer_order=customer_order,
            order_item=order_item,
            profile=profile,
            service_type=service_type,
            event_type=event_type,
            source=source,
            actor_type=actor_type,
            actor_phone=actor_phone,
            platform_code=platform_code,
            status_before=status_before,
            status_after=status_after,
            note=note,
            event_timestamp=utcnow(),
            payload=payload,
        )
        self.session.add(event)
        self.session.flush()
        return event
