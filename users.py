from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from auth import get_token_claims, hash_password, require_active_subject
from phone_utils import phone_lookup_candidates
from supabase_store import AdminUser, AppUser, AppUserTraveler, SupabaseStore, utcnow

# BE-6: persist only known account states/roles. Free-text values silently break
# downstream logic (e.g. _is_row_active only treats "active" as live).
_ALLOWED_ACCOUNT_STATUSES = {"active", "blocked", "suspended", "deleted"}
_ALLOWED_ADMIN_ROLES = {"admin", "super_admin", "owner"}


def _check_account_status(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in _ALLOWED_ACCOUNT_STATUSES:
        raise ValueError(f"status must be one of {sorted(_ALLOWED_ACCOUNT_STATUSES)}")
    return normalized


def _check_admin_role(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in _ALLOWED_ADMIN_ROLES:
        raise ValueError(f"role must be one of {sorted(_ALLOWED_ADMIN_ROLES)}")
    return normalized


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

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        return _check_account_status(v)


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

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        return _check_account_status(v)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        return _check_admin_role(v)


class AdminUserUpdatePayload(BaseModel):
    name: str | None = None
    is_loyalty: bool | None = Field(default=None, alias="isLoyalty")
    blocked: bool | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        return None if v is None else _check_account_status(v)

    model_config = {"populate_by_name": True}


class TravelerPayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    relation: str | None = None
    dob: str | None = None


class TravelerUpdatePayload(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    relation: str | None = None
    dob: str | None = None


def _serialize_traveler(row: AppUserTraveler) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "relation": row.relation,
        "dob": row.dob,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
    }


def register_user_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    def _require_admin_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser:
        row = require_active_subject(db, claims=claims, subject_type="admin")
        assert isinstance(row, AdminUser)
        return row

    def _require_active_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AdminUser | AppUser:
        row = require_active_subject(db, claims=claims)
        assert isinstance(row, (AdminUser, AppUser))
        return row

    def _require_user_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AppUser:
        row = require_active_subject(db, claims=claims, subject_type="user")
        assert isinstance(row, AppUser)
        return row

    @app.get("/api/v1/travelers/my")
    def list_my_travelers(
        db: Session = Depends(get_db),
        actor: AppUser = Depends(_require_user_actor),
    ) -> dict[str, Any]:
        rows = SupabaseStore(db).list_travelers(user_id=actor.id)
        return {"success": True, "data": {"travelers": [_serialize_traveler(r) for r in rows]}}

    @app.post("/api/v1/travelers/my")
    def create_my_traveler(
        payload: TravelerPayload,
        db: Session = Depends(get_db),
        actor: AppUser = Depends(_require_user_actor),
    ) -> dict[str, Any]:
        row = SupabaseStore(db).add_traveler(
            user_id=actor.id,
            name=payload.name,
            relation=payload.relation,
            dob=payload.dob,
        )
        return {"success": True, "data": {"traveler": _serialize_traveler(row)}}

    @app.patch("/api/v1/travelers/my/{traveler_id}")
    def update_my_traveler(
        traveler_id: int,
        payload: TravelerUpdatePayload,
        db: Session = Depends(get_db),
        actor: AppUser = Depends(_require_user_actor),
    ) -> dict[str, Any]:
        store = SupabaseStore(db)
        row = store.get_traveler(traveler_id=traveler_id, user_id=actor.id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Traveler not found")
        updated = store.update_traveler(row, name=payload.name, relation=payload.relation, dob=payload.dob)
        return {"success": True, "data": {"traveler": _serialize_traveler(updated)}}

    @app.delete("/api/v1/travelers/my/{traveler_id}")
    def delete_my_traveler(
        traveler_id: int,
        db: Session = Depends(get_db),
        actor: AppUser = Depends(_require_user_actor),
    ) -> dict[str, Any]:
        store = SupabaseStore(db)
        row = store.get_traveler(traveler_id=traveler_id, user_id=actor.id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Traveler not found")
        store.delete_traveler(row)
        return {"success": True, "data": {"deleted": True, "id": traveler_id}}

    def _soft_delete_user_row(user: AppUser) -> dict[str, Any]:
        if user.deleted_at is None or user.status != "deleted":
            user.status = "deleted"
            user.deleted_at = user.deleted_at or utcnow()
            user.updated_at = utcnow()
        return {
            "deleted": True,
            "id": user.id,
            "userId": user.id,
            "status": "deleted",
            "deletedAt": user.deleted_at,
        }

    @app.post("/api/v1/admin/users")
    def save_user(
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
    def list_users(
        db: Session = Depends(get_db),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = Query(default=None),
        include_deleted: bool = Query(default=False, alias="includeDeleted"),
        actor: AdminUser | AppUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        if isinstance(actor, AdminUser):
            query = select(AppUser).order_by(AppUser.updated_at.desc(), AppUser.created_at.desc())
            if not include_deleted:
                query = query.where(AppUser.deleted_at.is_(None), AppUser.status != "deleted")
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

    @app.patch("/api/v1/admin/users/{user_id}")
    def admin_update_user(
        user_id: str,
        payload: AdminUserUpdatePayload,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        row = db.scalar(select(AppUser).where(AppUser.id == user_id))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if payload.name is not None and payload.name.strip():
            row.name = payload.name.strip()
        if payload.is_loyalty is not None:
            row.is_loyalty = bool(payload.is_loyalty)
        if payload.blocked is not None:
            if payload.blocked:
                row.blocked_at = row.blocked_at or utcnow()
                row.status = "blocked"
            else:
                row.blocked_at = None
                if row.status == "blocked":
                    row.status = "active"
        if payload.status is not None and payload.status.strip():
            row.status = payload.status.strip().lower()
        row.updated_at = utcnow()
        db.commit()
        db.refresh(row)
        return {
            "user": {
                "id": row.id,
                "userId": row.id,
                "name": row.name,
                "phone": row.phone,
                "email": row.email,
                "status": row.status,
                "isLoyalty": bool(row.is_loyalty),
                "isBlocked": row.status == "blocked" or row.blocked_at is not None,
            }
        }

    @app.delete("/api/v1/admin/users/{user_id}")
    @app.delete("/admin/users/{user_id}")
    def delete_user_by_id(
        user_id: str,
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        user_row = db.scalar(select(AppUser).where(AppUser.id == user_id))
        if user_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        payload = _soft_delete_user_row(user_row)
        db.commit()
        return payload

    @app.delete("/api/v1/admin/users")
    @app.delete("/admin/users")
    def delete_user_by_lookup(
        user_id: str | None = Query(default=None, alias="userId"),
        phone: str | None = Query(default=None),
        db: Session = Depends(get_db),
        _: AdminUser = Depends(_require_admin_actor),
    ) -> dict[str, Any]:
        user_row: AppUser | None = None
        if user_id:
            user_row = db.scalar(select(AppUser).where(AppUser.id == user_id))
        elif phone:
            user_row = db.scalar(select(AppUser).where(AppUser.phone.in_(phone_lookup_candidates(phone))))
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Either userId or phone is required",
            )
        if user_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        payload = _soft_delete_user_row(user_row)
        db.commit()
        return payload

    @app.post("/api/v1/admin/admin-users")
    def save_admin_user(
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
    def list_admin_users(
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
