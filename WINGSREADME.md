# WINGS Availability Only

This backend now includes only the Eurowings/WINGS availability API from:

- `/Users/laveencompany/Projects/The Backend/backend/wings_single_file_hardcoded.py`

What was brought in:

- hardcoded WINGS live base URL
- hardcoded WINGS auth token
- availability request models
- search payload builder
- provider call logic
- merge/filter logic that keeps only `BASIC`, `SMART`, and `BIZ`

What was intentionally not brought in:

- booking route
- database usage
- persistence
- admin integration
- any extra WINGS features beyond availability

## Live routes added

- `GET /api/v1/wings/health`
- `POST /api/v1/wings/availability/raw`

## Hardcoded provider config

These values are currently hardcoded in [wings_api.py](/Users/laveencompany/Desktop/backendformobileapp/wings_api.py):

- `WINGS_BASE_URL = "https://wings.laveen-air.com/RIAM_main/rest/api"`
- `WINGS_SEARCH_URL = "https://wings.laveen-air.com/RIAM_main/rest/api/AirLowFareSearch"`
- `REQUEST_TIMEOUT_SECONDS = 60.0`
- `WINGS_AUTH_TOKEN` is hardcoded in the file

## Availability request body

`POST /api/v1/wings/availability/raw`

Example:

```json
{
  "from": "EBL",
  "to": "DUS",
  "date": "2026-05-01",
  "trip_type": "oneway",
  "cabin": "economy",
  "pax": {
    "adults": 1,
    "children": 0,
    "infants": 0
  }
}
```

Supported fields:

- `from`: origin IATA code
- `to`: destination IATA code
- `date`: departure date in `YYYY-MM-DD`
- `trip_type`: `oneway` or `roundtrip`
- `return_date`: required for roundtrip behavior
- `cabin`: accepted by the request, but the backend forces two provider searches:
  - `Economy`
  - `Business`
- `pax.adults`
- `pax.children`
- `pax.infants`

## How the availability search works

For every availability request, the backend:

1. builds one WINGS request for `Economy`
2. builds one WINGS request for `Business`
3. sends both to the provider
4. merges the results
5. filters the result down to only:
   - `BASIC`
   - `SMART`
   - `BIZ`
6. keeps the cheapest itinerary per class

If no `BASIC`, `SMART`, or `BIZ` fares are found, the response returns:

```json
{
  "errors": {
    "error": [
      {
        "value": "No BASIC/SMART/BIZ fares available for this search"
      }
    ]
  },
  "pricedItineraries": {
    "pricedItinerary": []
  }
}
```

## Response shape

The route returns:

```json
{
  "request": {
    "economy": {},
    "business": {},
    "note": "Merged and filtered to BASIC/SMART/BIZ only"
  },
  "response": {}
}
```

`request` shows the exact provider payloads sent.

`response` is the filtered provider payload.

## Health route response

`GET /api/v1/wings/health`

Example:

```json
{
  "ok": true,
  "service": "wings-availability",
  "base_url": "https://wings.laveen-air.com/RIAM_main/rest/api",
  "search_url": "https://wings.laveen-air.com/RIAM_main/rest/api/AirLowFareSearch",
  "token_hardcoded": true,
  "availability_only": true
}
```

## Files changed

- [wings_api.py](/Users/laveencompany/Desktop/backendformobileapp/wings_api.py)
- [app.py](/Users/laveencompany/Desktop/backendformobileapp/app.py)
- [WINGSREADME.md](/Users/laveencompany/Desktop/backendformobileapp/WINGSREADME.md)

## Run locally

From this backend repo:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Then test:

```bash
curl http://127.0.0.1:8000/api/v1/wings/health
```

```bash
curl -X POST http://127.0.0.1:8000/api/v1/wings/availability/raw \
  -H 'Content-Type: application/json' \
  -d '{
    "from": "EBL",
    "to": "DUS",
    "date": "2026-05-01",
    "trip_type": "oneway",
    "pax": {
      "adults": 1,
      "children": 0,
      "infants": 0
    }
  }'
```

## Important notes

- this is hardcoded live-provider access
- no database is used for WINGS availability
- no booking route was added
- no environment-variable refactor was done
- no auth wrapper was added around these WINGS routes
- this is availability only, exactly as requested
