from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth import get_token_claims, hash_password, require_active_subject
from supabase_store import AdminUser, AppUser, SupabaseStore


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
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        data = payload.model_dump(by_alias=False)
        password = data.pop("password", None)
        if password is not None:
            data["password_hash"] = hash_password(password)
        user = SupabaseStore(db).ensure_user(**data)
        db.commit()
        db.refresh(user)
        return {"user": {"id": user.id, "phone": user.phone, "name": user.name, "status": user.status}}

    @app.get("/api/v1/admin/users")
    async def list_users(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        actor: AdminUser | AppUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        store = SupabaseStore(db)
        if isinstance(actor, AdminUser):
            rows = store.list_rows(AppUser, exclude={"password_hash"}, limit=limit, offset=offset)
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
            normalized_rows.append({**row, "userId": user_id})
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
        return {"adminUsers": rows, "pagination": {"limit": limit, "offset": offset, "count": len(rows)}}
