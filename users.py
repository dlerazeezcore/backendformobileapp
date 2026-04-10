from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from auth import get_token_claims, hash_password, require_active_subject
from phone_utils import phone_lookup_candidates
from supabase_store import AdminUser, AppUser, SupabaseStore, utcnow


class UserPayload(BaseModel):
    phone: str
    name: str
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)
    status: str = "active"
    is_loyalty: bool = Field(default=False, alias="isLoyalty")
    notes: str | None = None
    blocked_at: datetime | None = Field(default=None, alias="blockedAt")
    deleted_at: datetime | None = Field(default=None, alias="deletedAt")
    last_login_at: datetime | None = Field(default=None, alias="lastLoginAt")


class AdminUserPayload(BaseModel):
    phone: str
    name: str
    email: str | None = None
    password: str | None = Field(default=None, min_length=8)
    status: str = "active"
    role: str = "admin"
    can_manage_users: bool = Field(default=False, alias="canManageUsers")
    can_manage_orders: bool = Field(default=False, alias="canManageOrders")
    can_manage_pricing: bool = Field(default=False, alias="canManagePricing")
    can_manage_content: bool = Field(default=False, alias="canManageContent")
    can_send_push: bool = Field(default=False, alias="canSendPush")
    notes: str | None = None
    blocked_at: datetime | None = Field(default=None, alias="blockedAt")
    deleted_at: datetime | None = Field(default=None, alias="deletedAt")
    last_login_at: datetime | None = Field(default=None, alias="lastLoginAt")
    custom_fields: dict[str, Any] = Field(default_factory=dict, alias="customFields")


def register_user_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    async def _require_admin_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        return row

    async def _require_active_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser | AppUser:
        row = require_active_subject(db, claims=claims)
        assert isinstance(row, (AdminUser, AppUser))
        return row

    @app.post("/api/v1/admin/users")
    async def save_user(
        payload: UserPayload,
        db: Session = Depends(get_db),
        actor: AdminUser | AppUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        data = payload.model_dump(by_alias=False)
        password = data.pop("password", None)
        if password is not None:
            data["password_hash"] = hash_password(password)

        if isinstance(actor, AppUser):
            actor_row = db.scalar(select(AppUser).where(AppUser.id == actor.id))
            if actor_row is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Auth subject not found")
            requested_phone = str(data.get("phone") or "").strip()
            if actor_row.phone not in phone_lookup_candidates(requested_phone):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You can only update your own user profile.",
                )

            actor_row.name = data.get("name") or actor_row.name
            actor_row.email = data.get("email")
            if "password_hash" in data:
                actor_row.password_hash = data["password_hash"]

            requested_status = str(data.get("status") or "").strip().lower()
            if requested_status == "deleted":
                actor_row.status = "deleted"
                actor_row.deleted_at = actor_row.deleted_at or utcnow()

            actor_row.updated_at = utcnow()
            user = actor_row
        else:
            user = SupabaseStore(db).ensure_user(**data)

        db.commit()
        db.refresh(user)
        return {"user": {"id": user.id, "phone": user.phone, "name": user.name, "status": user.status}}

    @app.get("/api/v1/admin/users")
    async def list_users(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = Query(default=None),
        actor: AdminUser | AppUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        if isinstance(actor, AdminUser):
            query = select(AppUser).order_by(AppUser.updated_at.desc(), AppUser.created_at.desc())
            normalized_search = str(search or "").strip()
            if normalized_search:
                compact = normalized_search.replace(" ", "").replace("-", "")
                phone_candidates = {compact}
                if compact and not compact.startswith("+"):
                    phone_candidates.add(f"+{compact}")
                phone_prefix_filters = [AppUser.phone.like(f"{candidate}%") for candidate in phone_candidates if candidate]
                query = query.where(
                    or_(
                        AppUser.name.ilike(f"%{normalized_search}%"),
                        *phone_prefix_filters,
                    )
                )
            rows_orm = db.scalars(
                query.offset(max(0, offset)).limit(max(1, min(limit, 500)))
            ).all()
            rows = []
            for user_row in rows_orm:
                rows.append(
                    {
                        column.name: getattr(user_row, column.name)
                        for column in user_row.__table__.columns
                        if column.name != "password_hash"
                    }
                )
        else:
            own_row = db.scalar(select(AppUser).where(AppUser.id == actor.id))
            filtered: list[dict[str, Any]] = []
            if own_row is not None:
                filtered.append(
                    {
                        column.name: getattr(own_row, column.name)
                        for column in own_row.__table__.columns
                        if column.name != "password_hash"
                    }
                )
            rows = filtered[offset : offset + max(1, min(limit, 500))]
        normalized_rows = []
        for row in rows:
            user_id = row.get("id")
            status_value = str(row.get("status") or "active")
            blocked_at = row.get("blocked_at")
            is_blocked = status_value == "blocked" or blocked_at is not None
            normalized_rows.append(
                {
                    **row,
                    "userId": user_id,
                    "status": status_value,
                    "isBlocked": bool(is_blocked),
                    "isLoyalty": bool(row.get("is_loyalty", False)),
                    "updatedAt": row.get("updated_at"),
                }
            )
        return {"users": normalized_rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}

    @app.post("/api/v1/admin/admin-users")
    async def save_admin_user(
        payload: AdminUserPayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        data = payload.model_dump(by_alias=False)
        password = data.pop("password", None)
        if password is not None:
            data["password_hash"] = hash_password(password)
        admin_user = SupabaseStore(db).ensure_admin_user(**data)
        db.commit()
        db.refresh(admin_user)
        return {
            "adminUser": {
                "id": admin_user.id,
                "phone": admin_user.phone,
                "name": admin_user.name,
                "status": admin_user.status,
                "role": admin_user.role,
            }
        }

    @app.get("/api/v1/admin/admin-users")
    async def list_admin_users(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_rows(AdminUser, exclude={"password_hash"}, limit=limit, offset=offset)
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            normalized_rows.append(
                {
                    **row,
                    "isLoyalty": bool(row.get("is_loyalty", False)),
                    "status": str(row.get("status") or "active"),
                    "blockedAt": row.get("blocked_at"),
                    "deletedAt": row.get("deleted_at"),
                }
            )
        return {"adminUsers": normalized_rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}
