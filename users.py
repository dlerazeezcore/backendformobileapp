from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from supabase_store import AdminUser, AppUser, SupabaseStore


class UserPayload(BaseModel):
    phone: str
    name: str
    email: str | None = None
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
    @app.post("/api/v1/admin/users")
    async def save_user(payload: UserPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        user = SupabaseStore(db).ensure_user(**payload.model_dump(by_alias=False))
        db.commit()
        db.refresh(user)
        return {"user": {"id": user.id, "phone": user.phone, "name": user.name, "status": user.status}}

    @app.get("/api/v1/admin/users")
    async def list_users(db: Session = Depends(get_db)) -> dict[str, Any]:
        return {"users": SupabaseStore(db).list_rows(AppUser)}

    @app.post("/api/v1/admin/admin-users")
    async def save_admin_user(payload: AdminUserPayload, db: Session = Depends(get_db)) -> dict[str, Any]:
        admin_user = SupabaseStore(db).ensure_admin_user(**payload.model_dump(by_alias=False))
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
    async def list_admin_users(db: Session = Depends(get_db)) -> dict[str, Any]:
        return {"adminUsers": SupabaseStore(db).list_rows(AdminUser)}
