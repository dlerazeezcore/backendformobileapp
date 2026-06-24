from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field

from auth import get_token_claims, require_active_subject
from config import get_settings
from supabase_store import AdminUser, AppUser

LOGGER = logging.getLogger("uvicorn.error")


def _wings_search_url() -> str:
    """Search endpoint: explicit override, else derived from the configured base URL."""
    settings = get_settings()
    return settings.wings_search_url or f"{settings.wings_base_url}/AirLowFareSearch"


class WingsPax(BaseModel):
    adults: int = Field(1, ge=0)
    children: int = Field(0, ge=0)
    infants: int = Field(0, ge=0)


class WingsAvailabilityRequest(BaseModel):
    from_: str = Field(..., alias="from", description="Origin IATA code (example: EBL)")
    to: str = Field(..., description="Destination IATA code (example: DUS)")
    date: str = Field(..., description="Departure date in YYYY-MM-DD")
    cabin: str = "economy"
    trip_type: str = "oneway"
    return_date: str | None = None
    pax: WingsPax = Field(default_factory=WingsPax)


def _build_search_payload(req: WingsAvailabilityRequest, cabin_values: list[str]) -> dict[str, Any]:
    od: list[dict[str, Any]] = [
        {
            "DepartureDateTime": {"value": req.date},
            "OriginLocation": {"LocationCode": req.from_.upper()},
            "DestinationLocation": {"LocationCode": req.to.upper()},
        }
    ]
    if req.trip_type == "roundtrip" and req.return_date:
        od.append(
            {
                "DepartureDateTime": {"value": req.return_date},
                "OriginLocation": {"LocationCode": req.to.upper()},
                "DestinationLocation": {"LocationCode": req.from_.upper()},
            }
        )

    return {
        "ProcessingInfo": {"SearchType": "STANDARD"},
        "OriginDestinationInformation": od,
        "TravelPreferences": [{"CabinPref": [{"Cabin": c} for c in cabin_values]}],
        "TravelerInfoSummary": {
            "AirTravelerAvail": [
                {
                    "PassengerTypeQuantity": [
                        {"Code": "ADT", "Quantity": req.pax.adults},
                        {"Code": "CHD", "Quantity": req.pax.children},
                        {"Code": "INF", "Quantity": req.pax.infants},
                    ]
                }
            ]
        },
    }


def _extract_priced_itineraries(resp: dict[str, Any]) -> list[dict[str, Any]]:
    items = ((resp or {}).get("pricedItineraries") or {}).get("pricedItinerary") or []
    if isinstance(items, list):
        return items
    if isinstance(items, dict):
        return [items]
    return []


def _first_res_book_code(pi: dict[str, Any]) -> str:
    try:
        odo = (
            (pi.get("airItinerary") or {})
            .get("originDestinationOptions", {})
            .get("originDestinationOption", [])
        )
        if isinstance(odo, dict):
            odo = [odo]
        segs = (odo[0] or {}).get("flightSegment", []) if odo else []
        if isinstance(segs, dict):
            segs = [segs]
        return str((segs[0] or {}).get("resBookDesigCode") or "").strip().upper() if segs else ""
    except Exception:
        return ""


def _total_amount_raw(pi: dict[str, Any]) -> float:
    try:
        fares = ((pi.get("airItineraryPricingInfo") or {}).get("itinTotalFare") or [])
        if isinstance(fares, dict):
            fares = [fares]
        total = ((fares[0] or {}).get("totalFare") or {}).get("amount")
        return float(total)
    except Exception:
        return float("inf")


def _only_basic_smart_biz(*provider_responses: dict[str, Any]) -> dict[str, Any]:
    allowed = {"BASIC", "SMART", "BIZ"}
    best_by_class: dict[str, dict[str, Any]] = {}

    for resp in provider_responses:
        for pi in _extract_priced_itineraries(resp):
            rbd = _first_res_book_code(pi)
            if rbd not in allowed:
                continue
            existing = best_by_class.get(rbd)
            if existing is None or _total_amount_raw(pi) < _total_amount_raw(existing):
                best_by_class[rbd] = pi

    ordered_codes = ["BASIC", "SMART", "BIZ"]
    priced = [best_by_class[c] for c in ordered_codes if c in best_by_class]
    if not priced:
        return {
            "errors": {"error": [{"value": "No BASIC/SMART/BIZ fares available for this search"}]},
            "pricedItineraries": {"pricedItinerary": []},
        }

    return {"pricedItineraries": {"pricedItinerary": priced}}


async def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    token = settings.wings_auth_token
    if not token:
        raise HTTPException(
            status_code=503,
            detail={"provider": "wings", "error": "WINGS_AUTH_TOKEN is not configured"},
        )
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.wings_request_timeout_seconds) as client:
        resp = await client.post(url, headers=headers, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    if resp.status_code >= 400:
        LOGGER.error(
            "WINGS upstream availability request failed: status=%s body=%s",
            resp.status_code,
            body,
        )
        raise HTTPException(
            status_code=502,
            detail={"provider": "wings", "error": "Upstream availability request failed"},
        )
    return body


def register_wings_routes(app: FastAPI, get_db: Callable[..., Any]) -> None:
    def _require_active_actor(
        claims: dict[str, Any] = Depends(get_token_claims),
        db: Session = Depends(get_db),
    ) -> AppUser | AdminUser:
        row = require_active_subject(db, claims=claims)
        assert isinstance(row, (AppUser, AdminUser))
        return row

    @app.get("/api/v1/wings/health")
    async def wings_health() -> dict[str, Any]:
        settings = get_settings()
        return {
            "ok": True,
            "service": "wings-availability",
            "base_url": settings.wings_base_url,
            "search_url": _wings_search_url(),
            "token_configured": bool(settings.wings_auth_token),
            "availability_only": True,
        }

    @app.post("/api/v1/wings/availability/raw")
    async def wings_availability_raw(
        req: WingsAvailabilityRequest,
        _: AppUser | AdminUser = Depends(_require_active_actor),
    ) -> dict[str, Any]:
        search_url = _wings_search_url()
        payload_economy = _build_search_payload(req, ["Economy"])
        payload_business = _build_search_payload(req, ["Business"])
        resp_economy, resp_business = await asyncio.gather(
            _post_json(search_url, payload_economy),
            _post_json(search_url, payload_business),
        )
        provider_response = _only_basic_smart_biz(resp_economy, resp_business)

        return {
            "request": {
                "economy": payload_economy,
                "business": payload_business,
                "note": "Merged and filtered to BASIC/SMART/BIZ only",
            },
            "response": provider_response,
        }
