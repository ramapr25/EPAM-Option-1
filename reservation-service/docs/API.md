# API reference

This is the full HTTP contract of the service. The base URL when run locally
is `http://127.0.0.1:8000`. FastAPI also serves an OpenAPI schema at
`/openapi.json` and interactive Swagger UI at `/docs`.

## Conventions

- **Content type:** request and response bodies are JSON.
- **IDs:** resource and reservation ids are server-generated UUID4 strings.
- **Times:** ISO-8601.
  - Inputs may carry any offset (for example `2030-01-01T11:00:00+02:00`)
    and are converted to UTC. Inputs without an offset are taken as UTC.
  - Outputs are always UTC with a `Z` suffix, for example
    `2030-01-01T09:00:00Z`.
- **Time windows:** half-open `[start, end)`. `end` must be strictly after
  `start`. A window ending at 10:00 and one starting at 10:00 do not overlap.
- **Authentication:** none in the prototype. The caller identifies itself
  with `client_id` in the body.

### Error format

Errors raised by the service (404 and 409) have a single string detail:

```json
{ "detail": "reservation 1234 not found" }
```

Validation errors (422) use FastAPI's standard list form:

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body"],
      "msg": "Value error, end must be after start",
      "input": { "...": "..." }
    }
  ]
}
```

| Status | When |
|--------|------|
| `422` | The body or query failed validation: a missing field, wrong type, `capacity < 1`, empty `name`, `kind` or `client_id`, `end <= start`, or an unknown `status` filter. |
| `404` | The resource or reservation id does not exist. |
| `409` | A booking would exceed capacity, or you tried to confirm a `cancelled` or `expired` reservation. |

---

## Schemas

### Resource

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (UUID) | Server-generated |
| `name` | string | Non-empty |
| `kind` | string | Non-empty free text, for example `room`, `seat` or `charging_slot` |
| `capacity` | integer ≥ 1 | Maximum number of reservations that may overlap **at any instant**. Default `1`. |

### Reservation

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (UUID) | Server-generated |
| `resource_id` | string | The resource being booked |
| `client_id` | string | Who made the booking |
| `start` | datetime (UTC) | Inclusive |
| `end` | datetime (UTC) | Exclusive |
| `status` | `held` \| `confirmed` \| `cancelled` \| `expired` | See the [state table](#status-lifecycle) |
| `created_at` | datetime (UTC) | When the hold was created |
| `expires_at` | datetime (UTC) \| null | Deadline for confirming. Set only while `held`. Becomes `null` on confirm or cancel. Keeps its value once `expired`. |
| `confirmed_at` | datetime (UTC) \| null | Set on confirm |
| `cancelled_at` | datetime (UTC) \| null | Set on cancel |

### Status lifecycle

| Status | Takes capacity? | Can move to |
|--------|-----------------|-------------|
| `held` | Yes, until `expires_at` | `confirmed`, `cancelled`, `expired` |
| `confirmed` | Yes | `cancelled` |
| `cancelled` | No | final |
| `expired` | No | final |

A hold becomes `expired` at `expires_at`, by default 120 seconds after
creation. From that moment it no longer blocks new bookings, and every read
reports it as `expired`, even before the background sweeper runs.

---

## Endpoints

### `POST /resources`

Creates a resource.

**Request body**

```json
{ "name": "Room A", "kind": "room", "capacity": 1 }
```

`capacity` is optional and defaults to `1`.

**Responses**

| Status | Body |
|--------|------|
| `201 Created` | [Resource](#resource) |
| `422` | Validation error |

```json
{ "id": "f38310f3-d93e-4909-8a92-f7748347e6a0", "name": "Room A", "kind": "room", "capacity": 1 }
```

---

### `GET /resources`

Lists all resources in creation order.

| Status | Body |
|--------|------|
| `200 OK` | Array of [Resource](#resource) |

---

### `POST /reservations`

Creates a **hold** on a resource for a time window. The hold takes the slot
immediately and expires after `HOLD_TTL_SECONDS` (default 120) unless it is
[confirmed](#post-reservationsidconfirm).

**Headers**

| Header | Required | Meaning |
|--------|----------|---------|
| `Idempotency-Key` | No | Any string. If this `client_id` has already **successfully** created a reservation with this key, the original reservation is returned and nothing new is created. |

**Request body**

```json
{
  "resource_id": "f38310f3-d93e-4909-8a92-f7748347e6a0",
  "client_id": "alice",
  "start": "2030-01-01T09:00:00Z",
  "end": "2030-01-01T10:00:00Z"
}
```

**Responses**

| Status | Meaning | Body |
|--------|---------|------|
| `201 Created` | A new hold was created | [Reservation](#reservation) with `status: "held"` |
| `200 OK` | Idempotent replay. The response also has the header `Idempotent-Replayed: true`. | The **original** reservation, in its current status |
| `404` | `resource_id` does not exist | `{"detail": "resource <id> not found"}` |
| `409` | The resource is full for some part of the window | `{"detail": "resource <id> is fully booked for [<start>, <end>)"}` |
| `422` | Validation error, including `end <= start` | Validation error |

```json
{
  "id": "dc84f068-8f0d-4f17-809c-05bb4702752d",
  "resource_id": "f38310f3-d93e-4909-8a92-f7748347e6a0",
  "client_id": "alice",
  "start": "2030-01-01T09:00:00Z",
  "end": "2030-01-01T10:00:00Z",
  "status": "held",
  "created_at": "2026-10-26T16:19:55.426384Z",
  "expires_at": "2026-10-26T16:21:55.426384Z",
  "confirmed_at": null,
  "cancelled_at": null
}
```

**How the conflict check works**

A request is rejected with 409 if, at any instant in `[start, end)`, the
number of active reservations on the resource plus this one would exceed
`capacity`. Active means `confirmed`, or `held` and not yet past
`expires_at`. Some examples for a resource with capacity 1 that has a
booking at 09:00–10:00:

| New window | Result |
|------------|--------|
| 09:30–10:30 | 409, because it overlaps |
| 10:00–11:00 | 201, because it only touches the end |
| 08:00–09:00 | 201, because it only touches the start |

With capacity 2 and existing bookings at 09–10 and 11–12, a new 09–12
booking is **accepted**. Never more than two bookings coexist at any instant.

Concurrent requests for the same resource are serialised, so two parallel
requests can never both win the last slot.

**Idempotency details**

- Keys are scoped per `client_id`. Alice's key `abc` and Bob's key `abc` are
  independent.
- Only successful creates (201) record the key. If a request fails with 404,
  409 or 422, you can retry the same key later and it is evaluated afresh.
- A replay ignores the new request body and returns whatever reservation the
  key created, in its current status. That might be `confirmed`,
  `cancelled` or `expired`. Use a fresh key for each distinct booking.

---

### `GET /reservations`

Lists reservations, sorted by `start` and then `created_at`.

**Query parameters.** All are optional and combine with AND.

| Param | Example | Notes |
|-------|---------|-------|
| `resource_id` | `?resource_id=f383...` | Exact match |
| `client_id` | `?client_id=alice` | Exact match |
| `status` | `?status=held` | One of `held`, `confirmed`, `cancelled` or `expired`. Anything else returns 422. |

Status filtering applies expiry in real time: a hold past its TTL appears
under `status=expired`, never under `status=held`.

| Status | Body |
|--------|------|
| `200 OK` | Array of [Reservation](#reservation). Empty if nothing matches. |
| `422` | Invalid `status` value |

---

### `GET /reservations/{id}`

| Status | Body |
|--------|------|
| `200 OK` | [Reservation](#reservation). A hold past its TTL is returned as `expired`. |
| `404` | `{"detail": "reservation <id> not found"}` |

---

### `POST /reservations/{id}/confirm`

Turns a hold into a confirmed reservation. There is no request body.

| Current status | Result |
|----------------|--------|
| `held`, before `expires_at` | `200`. Now `confirmed`, with `confirmed_at` set and `expires_at: null`. |
| `confirmed` | `200`, unchanged. Safe to retry. |
| `held` past `expires_at`, or `expired` | `409` `{"detail": "cannot confirm reservation in status 'expired'"}` |
| `cancelled` | `409` `{"detail": "cannot confirm reservation in status 'cancelled'"}` |
| unknown id | `404` |

---

### `DELETE /reservations/{id}`

Cancels a reservation and frees its slot immediately. There is no request
body.

| Current status | Result |
|----------------|--------|
| `held` or `confirmed` | `200`. Now `cancelled`, with `cancelled_at` set and `expires_at: null`. |
| `cancelled` | `200`, unchanged (no-op). Safe to retry. |
| `expired` | `200`, unchanged (no-op). It holds no slot anyway. |
| unknown id | `404` |

---

## Example session

This uses `curl` and `jq` in a POSIX shell. In PowerShell, use
`Invoke-RestMethod` with the same URLs and bodies.

```bash
BASE=http://127.0.0.1:8000

# 1. A room that fits one booking at a time
RID=$(curl -s -X POST $BASE/resources -H 'content-type: application/json' \
  -d '{"name":"Room A","kind":"room","capacity":1}' | jq -r .id)

# 2. Hold 09:00-10:00 (201, status "held"), sent with an idempotency key
R1=$(curl -s -X POST $BASE/reservations -H 'content-type: application/json' \
  -H 'Idempotency-Key: alice-001' \
  -d "{\"resource_id\":\"$RID\",\"client_id\":\"alice\",\"start\":\"2030-01-01T09:00:00Z\",\"end\":\"2030-01-01T10:00:00Z\"}" \
  | jq -r .id)

# 3. Retrying the same request returns 200, the same id and Idempotent-Replayed: true
curl -s -i -X POST $BASE/reservations -H 'content-type: application/json' \
  -H 'Idempotency-Key: alice-001' \
  -d "{\"resource_id\":\"$RID\",\"client_id\":\"alice\",\"start\":\"2030-01-01T09:00:00Z\",\"end\":\"2030-01-01T10:00:00Z\"}"

# 4. Confirm within 120s (200, status "confirmed")
curl -s -X POST $BASE/reservations/$R1/confirm

# 5. An overlapping request from someone else gets 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/reservations \
  -H 'content-type: application/json' \
  -d "{\"resource_id\":\"$RID\",\"client_id\":\"bob\",\"start\":\"2030-01-01T09:30:00Z\",\"end\":\"2030-01-01T10:30:00Z\"}"

# 6. List this room's confirmed reservations
curl -s "$BASE/reservations?resource_id=$RID&status=confirmed"

# 7. Cancel (200, status "cancelled"); repeating it is a no-op
curl -s -X DELETE $BASE/reservations/$R1
curl -s -X DELETE $BASE/reservations/$R1

# 8. Bob's 09:30-10:30 request now succeeds (201)
```

## Client guidance

- **Always send an `Idempotency-Key`** on `POST /reservations`, generated
  once per intended booking (a UUID works), and reuse it on network retries.
- **Treat 409 on create as final for that window.** Pick another time or
  resource. Retrying the same window succeeds only if something is cancelled
  or a hold expires.
- **Confirm promptly.** If the confirm returns 409 with status `expired`,
  create a new hold with a new idempotency key. The old key would replay the
  expired reservation.
- **Confirm and cancel are safe to retry.** Repeating either one returns the
  same final state.
