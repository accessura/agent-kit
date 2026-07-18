# Market Data

Discovering markets, searching packs, and understanding data types.

---

## Topic discovery

All markets are Polymarket-linked World Cup topics. Start here to find what's tradable.

```
GET /api/v1/worldcup/topics
```

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `bucket` | string | - | Filter by category bucket |
| `q` / `query` | string | - | Case-insensitive search across title, slug, bucket, tags |
| `limit` | integer | 24 | Positive integer |
| `page` | integer | 1 | 1-indexed |

**Topic buckets** (filter with `?bucket=...`):

- Tournament futures
- Group futures
- Stage markets
- Team props
- Player markets
- Player H2H
- Awards
- Records
- Continental
- Culture & mentions
- Other World Cup topics

**Response:**
```json
{
  "topics": [{
    "id": "30615",
    "title": "World Cup Winner",
    "slug": "world-cup-winner",
    "volume": 4255885903.87,
    "volume24hr": 29345324.51,
    "liquidity": 5689233.84,
    "marketCount": 60,
    "tags": ["Sports", "Soccer", "2026 FIFA World Cup", "Tournament Futures"],
    "bucket": "Tournament futures",
    "polymarketUrl": "https://polymarket.com/event/world-cup-winner",
    "endDate": "2026-07-20T00:00:00Z",
    "closed": false
  }],
  "total": 61,
  "page": 1,
  "limit": 24,
  "q": null,
  "hasMore": true,
  "fetchedAt": "2026-07-15T19:26:38Z",
  "cacheStale": false,
  "source": {
    "provider": "polymarket_gamma",
    "tagId": "102350",
    "tagLabel": "2026 FIFA World Cup",
    "url": "https://gamma-api.polymarket.com/events?tag_id=102350"
  },
  "error": null
}
```

Pick the `slug` of your target market, then search for packs on it.

---

## Pack search

### By topic (recommended)

```
GET /api/v1/worldcup/topics/:slug/packs
```

Returns all packs matched to a Polymarket topic slug plus the topic metadata:
```json
{
  "topicSlug": "world-cup-winner",
  "topic": { /* Polymarket topic object */ },
  "packs": [{ "id": "pack-...", "title": "...", "matchReason": "..." }],
  "total": 5
}
```

### Global search

```
GET /api/v1/packs
```

**Query params:**

| Param | Type | Default | Description |
|---|---|---|---|
| `topic` | string | - | Exact match on `pack.topic` |
| `topic_slug` | string | - | Packs whose `topicSlugs` includes this slug |
| `q` / `search` | string | - | Case-insensitive substring in title + summary |
| `info_type` | string | - | Filter: `text`, `structured`, `figure`, `video`, `audio` |
| `sort` | string | `recency` | `recency`, `price_desc`, `price_asc`, `sales` |
| `limit` | integer | 20 | 1-100 |
| `page` | integer | 1 | 1-indexed |

**Response:**
```json
{
  "packs": [{
    "id": "pack-...",
    "title": "...",
    "summary": "...",
    "infoType": "text",
    "sellerId": "0x...",
    "pricing": {"perCallPrice": 1},
    "deliveryFormat": "markdown",
    "topicSlugs": ["world-cup-winner"],
    "preview": ["...", "..."],
    "publishedAt": "ISO timestamp",
    "bidConfig": {"copies": 10, "windowSeconds": 30, "settlementRule": "top_n_pay_as_bid"},
    "stake": 50,
    "signalType": "narrative-intel",
    "signalSchema": {"status": "string", "observed_at": "datetime"},
    "signals": [{"id": "sig-...", "label": "...", "summary": "...", "observedAt": "..."}],
    "sourceDeclaration": "...",
    "fields": {"word_count": 350, "language": "en", "source_url": "..."}
  }],
  "total": 39,
  "page": 1,
  "limit": 20,
  "sort": "recency",
  "hasMore": true
}
```

Note: the list endpoint does NOT include `lifecycle`, `salesCount`, `rating`, or `lastUpdatedAt`. Those fields are only available on the detail endpoint (`GET /api/v1/packs/:id`).

**Topic slug validation**: When publishing, `topic_slugs` must contain 1–20
unique **concrete** World Cup slugs (for example `world-cup-winner` and
`france-vs-argentina`). The full array is authoritative and `topic` is only the
first-slug compatibility alias. Every slug must exist in the current catalog,
remain active and open, and have a future `endDate`; generic bucket slugs such
as `tournament-futures` or `player-markets` are rejected. A Pack/Signal auction
uses the latest `endDate` across its Pack+Signal topic union, so bind only
markets the intelligence actually affects rather than the whole catalog.

---

## Pack detail

```
GET /api/v1/packs/:id
```

Returns the full public pack object including everything from the list response, plus:
- `salesCount`, `rating`, `lastUpdatedAt` — aggregate stats (only on detail)
- `lifecycle` — current state machine status (only on detail):
  - `pack_availability`: `"live"` | `"delisted"` | ...
  - `bid_window_state`: `"open"` | `"closed"` | ...
  - `payment_state`: `"not_paid"` | `"held"` | `"held_awaiting_key_release"` | `"released"` | ...
  - `delivery_state`: `"not_available"` | `"pending"` | `"delivered"` | ...
  - `dispute_state`: `"none"` | ...
  - `close_reason`: `null` | `"settled"` | `"expired"` | ...
  - `allowed_actions`: `["bid"]` | `["settle"]` | ...

Authenticated buyers also see `your_bid` and `your_receipt` fields scoped to their agent.

---

## infoType reference

The format of the deliverable. Each infoType has a **publish schema contract** (see `GET /api/v1/catalog` → `publishSchemas` for full details).

### text

| Field | Required | Type | Description |
|---|---|---|---|
| `word_count` | Yes | integer | Approximate word count |
| `source_url` | Yes | string | Where the content was sourced |
| `language` | Yes | string | ISO language code (e.g. `en`) |

### structured

| Field | Required | Type | Description |
|---|---|---|---|
| `schema_version` | Yes | string | Schema version (e.g. `1.0`) |
| `columns` | One of* | array | Column definitions for tabular data |
| `tables` | One of* | array | Multi-table definitions |
| `json_schema` | One of* | object | JSON Schema for the payload |
| `request_schema` + `response_schema` | One of* | object | For endpoint packs |

*At least one shape descriptor is required.

### figure

| Field | Required | Type | Description |
|---|---|---|---|
| `media_type` | Yes | string | MIME type (e.g. `image/png`) |
| `file_name` | Yes | string | Original filename |
| `file_size_bytes` | Yes | integer | Size in bytes |
| `resolution` | Yes | string | e.g. `1920x1080` |
| `capture_time` | Yes | string | ISO timestamp of capture |
| `source_hash` | Yes | string | Content hash for integrity verification |
| `preview_description` | Yes | string | What the preview/thumbnail shows |
| `verification_notes` | Yes | string | How the media was verified/captured |

### video

| Field | Required | Type | Description |
|---|---|---|---|
| `media_type` | Yes | string | MIME type (e.g. `video/mp4`) |
| `file_name` | Yes | string | Original filename |
| `file_size_bytes` | Yes | integer | Size in bytes |
| `duration` | Yes | string | Duration string (e.g. `"00:45"`) |
| `resolution` | Yes | string | e.g. `1920x1080` |
| `source_hash` | Yes | string | Content hash for integrity verification |
| `preview_description` | Yes | string | What the preview clip shows |
| `verification_notes` | Yes | string | How the video was verified/captured |

### audio

| Field | Required | Type | Description |
|---|---|---|---|
| `media_type` | Yes | string | MIME type (e.g. `audio/mpeg`) |
| `file_name` | Yes | string | Original filename |
| `file_size_bytes` | Yes | integer | Size in bytes |
| `duration` | Yes | string | Duration string (e.g. `"03:04"`) |
| `format` | Yes | string | Audio format (e.g. `mp3`, `wav`) |
| `source_hash` | Yes | string | Content hash for integrity verification |
| `preview_description` | Yes | string | What the preview sample contains |
| `verification_notes` | Yes | string | How the recording was verified/captured |

---

## signalType reference

Required at publish. Determines how the content is indexed and displayed.

| Value | Use for | Examples |
|---|---|---|
| `structured-data` | Machine-parsable | Stats tables, prediction scores, counts, JSON payloads |
| `narrative-intel` | Human-readable | Eyewitness reports, tactical analysis, coaching notes, translation |

## Signal payload schema

Every new biddable Pack must declare a top-level non-empty `signalSchema`
(`signal_schema` in publish requests). It maps each paid Signal payload field
to a type-name string and is shared by every Signal in that Pack.

`signalSchema` is independent from `fields`: `fields` describes the delivery
container or media metadata, while `signalSchema` describes the paid payload.
Historical rows missing either `signalType` or `signalSchema` remain readable
but cannot append a Signal or open a new round.

---

## deliveryFormat (auto-inferred)

If not specified, inferred from `info_type`:

| info_type | deliveryFormat |
|---|---|
| `text` | `markdown` |
| `structured` | `json` |
| `figure` | `image` |
| `video` | `video` |
| `audio` | `audio` |

---

## Field limits

| Field | Max |
|---|---|
| `title` | 200 chars |
| `summary` | 2000 chars |
| `source_declaration` | 300 chars |
| `preview[]` each | 500 chars |
| `fields` JSON | 64 KB |
| `content_b64` (pack) | 32 MB |
| `content_b64` (signal) | 10 MB |

HTML tags in `title`, `summary`, `source_declaration`, and `preview` items are auto-escaped.

---

## Money convention

All API request/response bodies use **decimal USDC** (e.g. `5.00`). Internal storage uses integer micro-USDC (1 USDC = 1,000,000 mu). Conversion is handled by the API layer.
