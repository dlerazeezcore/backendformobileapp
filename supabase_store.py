from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, Uuid, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    is_loyalty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_orders: Mapped[list["CustomerOrder"]] = relationship(back_populates="user")
    profiles: Mapped[list["ESimProfile"]] = relationship(back_populates="user")


class AdminUser(TimeMixin, Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4()))
    phone: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255))
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
    booked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped[AppUser | None] = relationship(back_populates="customer_orders")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="customer_order")
    lifecycle_events: Mapped[list["ESimLifecycleEvent"]] = relationship(back_populates="customer_order")
    payload_snapshots: Mapped[list["ProviderPayloadSnapshot"]] = relationship(back_populates="customer_order")


class OrderItem(TimeMixin, Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_order_id: Mapped[int] = mapped_column(ForeignKey("customer_orders.id", ondelete="CASCADE"), index=True)
    service_type: Mapped[str] = mapped_column(String(32), default="esim", nullable=False, index=True)
    item_status: Mapped[str] = mapped_column(String(80), default="BOOKED", nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(80), default="esim_access", nullable=False)
    provider_order_no: Mapped[str | None] = mapped_column(String(120), unique=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    purchase_channel: Mapped[str | None] = mapped_column(String(80))
    booked_via_platform: Mapped[str | None] = mapped_column(String(80))
    canceled_via_platform: Mapped[str | None] = mapped_column(String(80))
    refunded_via_platform: Mapped[str | None] = mapped_column(String(80))
    revoked_via_platform: Mapped[str | None] = mapped_column(String(80))
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
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
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

    def ensure_user(
        self,
        *,
        phone: str,
        name: str,
        email: str | None = None,
        status: str = "active",
        is_loyalty: bool = False,
        notes: str | None = None,
        blocked_at: datetime | None = None,
        deleted_at: datetime | None = None,
        last_login_at: datetime | None = None,
    ) -> AppUser:
        user = self.session.scalar(select(AppUser).where(AppUser.phone == phone))
        if user is None:
            user = AppUser(phone=phone, name=name)
            self.session.add(user)
        user.phone = phone
        user.name = name
        user.email = email
        user.status = status
        user.is_loyalty = is_loyalty
        user.notes = notes
        user.blocked_at = blocked_at
        user.deleted_at = deleted_at
        user.last_login_at = last_login_at
        self.session.flush()
        return user

    def ensure_admin_user(
        self,
        *,
        phone: str,
        name: str,
        email: str | None = None,
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
        admin_user = self.session.scalar(select(AdminUser).where(AdminUser.phone == phone))
        if admin_user is None:
            admin_user = AdminUser(phone=phone, name=name)
            self.session.add(admin_user)
        admin_user.phone = phone
        admin_user.name = name
        admin_user.email = email
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
        rows = self.session.scalars(
            select(ExchangeRate).where(
                ExchangeRate.base_currency == base_currency,
                ExchangeRate.quote_currency == quote_currency,
                ExchangeRate.active.is_(True),
            )
        ).all()
        eligible = [
            row
            for row in rows
            if row.effective_at <= now and (row.expires_at is None or row.expires_at > now)
        ]
        eligible.sort(key=lambda row: (row.effective_at, row.id), reverse=True)
        return eligible[0] if eligible else None

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
        rows = self.session.scalars(
            select(PricingRule).where(
                PricingRule.service_type == service_type,
                PricingRule.active.is_(True),
            )
        ).all()
        eligible: list[PricingRule] = []
        for row in rows:
            if row.starts_at and row.starts_at > now:
                continue
            if row.ends_at and row.ends_at <= now:
                continue
            if row.rule_scope == "package" and row.package_code != package_code:
                continue
            if row.rule_scope == "country" and row.country_code != country_code:
                continue
            if row.rule_scope == "provider" and row.provider_code != provider_code:
                continue
            eligible.append(row)
        eligible.sort(
            key=lambda row: (
                self._rule_specificity(
                    rule_scope=row.rule_scope,
                    package_code=row.package_code,
                    country_code=row.country_code,
                    provider_code=row.provider_code,
                ),
                row.priority,
                row.created_at,
                row.id,
            ),
            reverse=True,
        )
        return eligible[0] if eligible else None

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
        rows = self.session.scalars(
            select(DiscountRule).where(
                DiscountRule.service_type == service_type,
                DiscountRule.active.is_(True),
            )
        ).all()
        eligible: list[DiscountRule] = []
        for row in rows:
            if row.starts_at and row.starts_at > now:
                continue
            if row.ends_at and row.ends_at <= now:
                continue
            if row.rule_scope == "package" and row.package_code != package_code:
                continue
            if row.rule_scope == "country" and row.country_code != country_code:
                continue
            if row.rule_scope == "provider" and row.provider_code != provider_code:
                continue
            eligible.append(row)
        eligible.sort(
            key=lambda row: (
                self._rule_specificity(
                    rule_scope=row.rule_scope,
                    package_code=row.package_code,
                    country_code=row.country_code,
                    provider_code=row.provider_code,
                ),
                row.priority,
                row.created_at,
                row.id,
            ),
            reverse=True,
        )
        return eligible[0] if eligible else None

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
        purchase_channel: str | None = None,
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
        custom_fields: dict[str, Any] | None = None,
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
        item.purchase_channel = purchase_channel
        item.booked_via_platform = platform_code
        item.provider_status = "SUCCESS" if provider_response.get("success") else "FAILED"
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
        self.session.commit()
        self.session.refresh(order)
        self.session.refresh(item)
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
            total_data_mb = parse_provider_int(item.get("totalVolume"))
            used_data_mb = parse_provider_int(item.get("orderUsage"))
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
            total_data_mb = parse_provider_int(record.get("totalData"))
            used_data_mb = parse_provider_int(record.get("dataUsage"))
            remaining_data_mb = None
            if total_data_mb is not None and used_data_mb is not None:
                remaining_data_mb = max(total_data_mb - used_data_mb, 0)
            profile.total_data_mb = total_data_mb if total_data_mb is not None else profile.total_data_mb
            profile.used_data_mb = used_data_mb
            profile.remaining_data_mb = remaining_data_mb
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
                order_item.canceled_via_platform = platform_code
            if customer_order:
                customer_order.order_status = "CANCELLED"
        elif action == "revoke":
            profile.app_status = "REVOKED"
            profile.revoked_at = now
            if order_item:
                order_item.item_status = "REVOKED"
                order_item.revoked_at = now
                order_item.revoked_via_platform = platform_code
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
                order_item.refunded_via_platform = platform_code
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
        row = PricingRule(**payload)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_discount_rule(self, payload: dict[str, Any]) -> DiscountRule:
        row = DiscountRule(**payload)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_featured_location(self, payload: dict[str, Any]) -> FeaturedLocation:
        row = FeaturedLocation(**payload)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_exchange_rate(self, payload: dict[str, Any]) -> ExchangeRate:
        row = ExchangeRate(**payload)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_rows(self, model: Any) -> list[dict[str, Any]]:
        rows = self.session.scalars(select(model).order_by(model.created_at.desc())).all()
        result = []
        for row in rows:
            result.append({column.name: getattr(row, column.name) for column in row.__table__.columns})
        return result

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
