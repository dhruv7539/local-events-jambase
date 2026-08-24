# Live Events

Find live music events near a location. FastAPI backend over the JamBase v3 API,
with a small static single-page UI.

## Requirements

- Python 3.11+
- A JamBase API key (https://data.jambase.com)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and set your key
```

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `JAMBASE_API_KEY` | **yes** | — | Sent as `Authorization: Bearer <key>` |
| `JAMBASE_BASE_URL` | no | `https://api.data.jambase.com/v3` | Override for testing |
| `CONNECT_TIMEOUT` | no | `5.0` | Seconds to establish a connection |
| `READ_TIMEOUT` | no | `10.0` | Seconds to wait for a response body |
| `EVENT_CACHE_TTL` | no | `300` | Event result cache, seconds |
| `CITY_CACHE_TTL` | no | `86400` | City-name→ID cache, seconds |
| `RESULTS_PER_PAGE` | no | `60` | Events requested upstream (single page) |

`.env` is gitignored and has been since the first commit.

## Run

```bash
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000.

## Endpoints

### `GET /events`

| Param | Type | Default | Notes |
|---|---|---|---|
| `location` | string | *required* | `Austin` or `Austin, TX`. 1–100 chars. |
| `days` | int | `7` | 1–90, inclusive of today. `days=1` is today only. |

```bash
curl "http://127.0.0.1:8000/events?location=Austin,%20TX&days=7"
```

```json
{
  "location": "Austin, TX",
  "days": 7,
  "count": 57,
  "events": [
    {
      "id": "jambase:16725623",
      "name": "Bob Schneider at Saxon Pub",
      "url": "https://www.jambase.com/show/bob-schneider-saxon-pub-20260824",
      "status": "scheduled",
      "previous_date": null,
      "event_date": "2026-08-24",
      "event_time": "20:30:00",
      "venue": {
        "name": "Saxon Pub",
        "city": "Austin",
        "region": "TX",
        "street_address": "1320 South Lamar",
        "capacity": 150
      },
      "performers": [
        { "name": "Bob Schneider", "is_headliner": true, "genres": ["folk", "indie", "pop"] }
      ],
      "ticket_url": "https://www.eventbrite.com/e/1993553287548?aff=ebdijbjambase&utm_source=jambase",
      "price_range": { "min_price": "15.00", "max_price": "25.00", "currency": "USD" },
      "headliner": "Bob Schneider",
      "genres": ["folk", "indie", "pop"]
    }
  ]
}
```

`event_time` is `null` when JamBase publishes a date but no showtime.
`price_range` is `null` when no ticket offer publishes a price, or when priced
offers disagree on currency — it is never `0` and never a guess.

**Error responses** are `{"detail": "..."}`:

| Status | When |
|---|---|
| `404` | Location matched no known place |
| `422` | `days` outside 1–90, or `location` empty |
| `500` | Our API credentials were rejected |
| `502` | Upstream returned an error or was unreachable |
| `504` | Upstream exceeded the read timeout |

### `GET /health`

```bash
curl http://127.0.0.1:8000/health   # {"status":"ok"}
```

Liveness only — it does **not** call JamBase, so an upstream outage doesn't
fail our health check or burn API quota on every probe.

### `GET /`

Serves the single-page UI, which consumes `GET /events` over `fetch` exactly as
any external client would.

## Tests

```bash
pytest
```

24 tests, no network — the transport is stubbed with `httpx.MockTransport`, and
the fixture is a real captured JamBase response.

## Interactive API docs

FastAPI serves OpenAPI docs at http://127.0.0.1:8000/docs.
