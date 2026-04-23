from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Hardcoded live Eurowings/WINGS config copied from the provided single-file API.
WINGS_BASE_URL = "https://wings.laveen-air.com/RIAM_main/rest/api"
WINGS_AUTH_TOKEN = "Q0QwN0ExMDkxRkNCRjRDRjJGOUZFRjgwNzdEQzI1OTM="
WINGS_SEARCH_URL = f"{WINGS_BASE_URL}/AirLowFareSearch"
REQUEST_TIMEOUT_SECONDS = 60.0


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
    headers = {
        "Authorization": WINGS_AUTH_TOKEN,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, headers=headers, json=payload)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail={"provider": "wings", "status_code": resp.status_code, "response": body},
        )
    return body


def register_wings_routes(app: FastAPI) -> None:
    @app.get("/api/v1/wings/health")
    async def wings_health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "wings-availability",
            "base_url": WINGS_BASE_URL,
            "search_url": WINGS_SEARCH_URL,
            "token_hardcoded": True,
            "availability_only": True,
        }

    @app.post("/api/v1/wings/availability/raw")
    async def wings_availability_raw(req: WingsAvailabilityRequest) -> dict[str, Any]:
        payload_economy = _build_search_payload(req, ["Economy"])
        payload_business = _build_search_payload(req, ["Business"])
        resp_economy = await _post_json(WINGS_SEARCH_URL, payload_economy)
        resp_business = await _post_json(WINGS_SEARCH_URL, payload_business)
        provider_response = _only_basic_smart_biz(resp_economy, resp_business)

        return {
            "request": {
                "economy": payload_economy,
                "business": payload_business,
                "note": "Merged and filtered to BASIC/SMART/BIZ only",
            },
            "response": provider_response,
        }
