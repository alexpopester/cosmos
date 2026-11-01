# openc3-cosmos-json-tlm

Push telemetry into COSMOS as **JSON over HTTP** — no binary packing, no custom
accessor. You POST a JSON object naming a telemetry packet that already exists in
a target definition and the values for its items; COSMOS builds the real binary
packet from that definition and runs it through the **normal pipeline**: raw
logging, decommutation, limits, derived items, reingest and packet counts all
behave exactly as if the data had arrived over a binary link. Your target
definitions do not change.

- [How it works](#how-it-works)
- [Quickstart (demo target)](#quickstart-demo-target)
- [Which server / URL do I hit?](#which-server--url-do-i-hit)
- [Endpoints](#endpoints)
- [Request body reference](#request-body-reference)
- [Authentication modes](#authentication-modes)
- [Configuration reference](#configuration-reference)
- [Using it on other transports (MQTT/TCP)](#using-it-on-other-transports-mqtttcp)
- [Behavior & gotchas](#behavior--gotchas)
- [Throughput & concurrency](#throughput--concurrency)
- [Errors & troubleshooting](#errors--troubleshooting)
- [Running the tests](#running-the-tests)

## How it works

The plugin ships two things that share one conversion core
(`build_packet_from_json`):

1. **`JsonTlmServerInterface`** (the main mode) — a COSMOS *interface* that runs a
   small HTTP server. You POST JSON to it; it validates against the packet
   definition, builds the packet, and queues it into COSMOS.
2. **`JsonPacketProtocol`** — a READ protocol you can attach to *any* other
   interface whose payload is JSON (MQTT, TCP, …). Same conversion, different
   transport. See [that section](#using-it-on-other-transports-mqtttcp).

The packet must already be defined in a target (`TELEMETRY <TGT> <PKT> …`). This
plugin fills a defined packet; it never invents one.

## Quickstart (demo target)

The plugin includes a self-contained `JSONDEMO` target so you can try everything
before touching your own targets.

```bash
# 1. Build the plugin gem
cd openc3-cosmos-json-tlm
rake build VERSION=1.0.0                 # or: openc3.sh cli rake build VERSION=1.0.0

# 2. Create the auth secret (TOKEN mode default). Admin > Secrets in the UI, or:
#    POST /openc3-api/secrets/JSON_TLM_TOKEN  with {"value":"my-token"} (needs a session token)
#    (In OIDC mode you skip this — see Authentication modes.)

# 3. Install the gem: Admin > Plugins > upload, keep defaults
#    (json_tlm_targets=JSONDEMO, include_demo_target=true, json_tlm_auth_mode=TOKEN)

# 4. Post telemetry through COSMOS
curl -X POST http://localhost:2900/json-tlm/tlm/JSONDEMO/STATUS \
  -H "Authorization: Bearer my-token" -H "Content-Type: application/json" \
  -d '{"items": {"COUNTER": 42, "TEMP": 12000, "MODE": "SCIENCE", "LABEL": "livedemo"}}'
# -> 202 {"accepted": 1}

# 5. Read it back (TEMP 12000 raw -> 20.0 C via the definition's read conversion)
#    in Packet Viewer / Telemetry Grapher, or via the API.
```

## Which server / URL do I hit?

The interface opens a plain HTTP listener on `LISTEN_ADDRESS:PORT` **inside the
operator container** (default `0.0.0.0:7780`). You normally do **not** hit that
port directly. Instead, the `PORT` and `ROUTE_PREFIX` lines in `plugin.txt` tell
COSMOS to publish it through the **Traefik** reverse proxy, so the real URL you
POST to is:

```
http://<cosmos-host>:2900<route_prefix>/...        # e.g. http://localhost:2900/json-tlm/...
https://<cosmos-host><route_prefix>/...            # production, behind TLS on 443
```

So with the defaults (`json_tlm_port 7780`, `json_tlm_route_prefix /json-tlm`):

| You call | Goes to |
|---|---|
| `http://localhost:2900/json-tlm/health` | interface `:7780` `/health` |
| `http://localhost:2900/json-tlm/tlm/JSONDEMO/STATUS` | interface `:7780` `/tlm/JSONDEMO/STATUS` |

Notes:
- **Use the Traefik URL** (`:2900<prefix>`): it's the one reachable from outside
  the cluster and the one that carries TLS in production.
- **One interface = one port + one prefix.** Installing a second JSON interface
  needs its own `json_tlm_port` and `json_tlm_route_prefix`.
- Your sender needs network access to the COSMOS host on 2900 (or 443). The
  interface itself is HTTP; TLS is terminated by Traefik.

## Endpoints

Paths below are relative to the route prefix (`/json-tlm` by default).

### `GET /health` — liveness (no auth)

```bash
curl http://localhost:2900/json-tlm/health
```
```json
{ "status": "ok", "interface": "JSON_TLM_INT", "queue": 0, "targets": ["JSONDEMO"] }
```
`status` is `ok` when connected, queue is the current backlog depth.

### `GET /tlm/<TGT>/<PKT>` — discover what a packet expects (auth required)

Lists every **settable** item (DERIVED items are omitted) with its type, size,
states, units, and whether it has a read/write conversion — so you know whether
to send `RAW` or `CONVERTED` and what state strings are valid.

```bash
curl http://localhost:2900/json-tlm/tlm/JSONDEMO/STATUS -H "Authorization: Bearer my-token"
```
```json
{
  "target": "JSONDEMO", "packet": "STATUS",
  "items": [
    {"name":"COUNTER","data_type":"UINT","bit_size":32,"array_size":null,"states":null,
     "read_conversion":false,"write_conversion":false,"units":null},
    {"name":"TEMP","data_type":"UINT","bit_size":16,"array_size":null,"states":null,
     "read_conversion":true,"write_conversion":true,"units":"C"},
    {"name":"MODE","data_type":"UINT","bit_size":8,"array_size":null,
     "states":["SAFE","NOMINAL","SCIENCE"],"read_conversion":false,"write_conversion":false,"units":null}
  ]
}
```

### `POST /tlm/<TGT>/<PKT>` — one packet (auth required)

Target and packet come from the URL. Body is `{"items": {...}}` (or a flat
object of item names, see [body reference](#request-body-reference)).

```bash
curl -X POST http://localhost:2900/json-tlm/tlm/JSONDEMO/STATUS \
  -H "Authorization: Bearer my-token" -H "Content-Type: application/json" \
  -d '{"items": {"COUNTER": 42, "TEMP": 12000, "MODE": "SCIENCE"}}'
```
```json
{ "accepted": 1 }      // HTTP 202
```

### `POST /tlm` — batch (auth required)

Each entry names its own `target` and `packet`. Accepts either
`{"packets": [ ... ]}` or a bare JSON array. **All-or-nothing**: if any entry is
invalid the whole batch is rejected and nothing is queued.

```bash
curl -X POST http://localhost:2900/json-tlm/tlm \
  -H "Authorization: Bearer my-token" -H "Content-Type: application/json" \
  -d '{"packets": [
        {"target":"JSONDEMO","packet":"STATUS","items":{"COUNTER":100}},
        {"target":"JSONDEMO","packet":"STATUS","items":{"COUNTER":200}}
      ]}'
```
```json
{ "accepted": 2 }      // HTTP 202
```

## Request body reference

```jsonc
{
  "target": "INST",            // required in a /tlm batch; ignored on /tlm/<TGT>/<PKT> (URL wins)
  "packet": "HEALTH_STATUS",   // same as target
  "type":   "RAW",             // RAW (default) or CONVERTED — see below
  "items":  {                  // required: the values to write
    "TEMP1":  12000,           // number -> numeric item
    "MODE":   "SAFE",          // string -> STATE name (CONVERTED) or STRING item
    "WHEELS": [1, -2, 3, -4]   // array  -> ARRAY item
  },
  "received_time": 1760000000000000000, // optional: integer ns since epoch, OR ISO-8601 string
  "stored": false              // optional: true = historical data (logged, but skips the CVT)
}
```

**Flat form** — if you omit `items`, any keys that aren't the reserved words
(`target`, `packet`, `type`, `items`, `received_time`, `stored`) are treated as
item names. These are equivalent:

```json
{"items": {"COUNTER": 5}}
{"COUNTER": 5}
```

**`type`: RAW vs CONVERTED**
- `RAW` (default) — values are written straight into the binary fields. Send
  `12000` for a temperature whose read conversion turns it into `20.0 C`.
- `CONVERTED` — values are engineering values. Items with a **write conversion**
  are back-converted; **state names** are resolved to their numbers (`"SAFE"` →
  `0`). An item that has a *read* conversion but **no write** conversion is
  rejected in CONVERTED mode (COSMOS would otherwise apply the read conversion a
  second time and silently corrupt the value). Send it RAW, add a
  `WRITE_CONVERSION`, or disable the check with `OPTION STRICT_CONVERTED FALSE`.

**`received_time`** — integer = nanoseconds since the Unix epoch; string =
ISO-8601 (`"2026-10-08T12:00:00Z"`). Omit to let COSMOS stamp receipt time.

**`stored`** — `true` marks the packet as historical: it's logged and decommutated
but does **not** update the current value table (so it won't overwrite live data).

**BLOCK values** — JSON has no bytes type, so a BLOCK item accepts a string
(UTF-8 encoded), a list of byte values (`[1, 2, 255]`), or COSMOS's own JSON
binary form (`{"json_class": "String", "raw": [1, 2, 255]}`).

### Dynamic and variable length items

Both kinds of dynamic item work with no definition changes. Send the data; the
packet grows, shrinks and shifts to fit.

- **Legacy dynamic items** — a `0` bit size / array size ("rest of the packet")
  or a negative one ("rest of the packet minus N bits", e.g. before a CRC). The
  value's length sets the packet length.
- **`VARIABLE_BIT_SIZE` items** — the size comes from a length item:
  `length × bits_per_count + bit_offset`. The interface **fills the length item
  in for you** from the value, and every item after it moves along. Works for
  STRING (UTF-8 byte length), BLOCK, arrays of any element size, and QUIC
  variable length INT/UINT (the length code 0–3 is picked from the value).
  - Don't send the length item. If you do, it's only checked: a value that
    doesn't match the data is a `400`, so a sender can assert its own length.
  - A value whose size can't be expressed by the length item (e.g. 3 bytes when
    `bits_per_count` is 32) is a `400`.
  - Unsent variable items are **empty** (`MISSING_ITEMS DEFAULTS`) or keep the
    previous packet's value (`LAST`).

Using the demo target's `VARIABLE` packet:

```bash
curl -X POST http://localhost:2900/json-tlm/tlm/JSONDEMO/VARIABLE \
  -H "Authorization: Bearer my-token" -H "Content-Type: application/json" \
  -d '{"items": {"NAME": "héllo", "SAMPLES": [1, -2, 3], "BLOB": [1, 2, 3], "VINT": 1000, "TRAILER": 7}}'
# NAME_LEN=6 (UTF-8 bytes), COUNT=3, BLOB_LEN=5 (3 bytes + the -16 bit offset), VINT_LEN=1
```

`GET /tlm/<TGT>/<PKT>` marks these items with `"dynamic": true`, includes each
variable item's `variable_bit_size`, and marks length items with `"length_of"`.

### JSON-accessor packets

A packet defined with `ACCESSOR JsonAccessor` (a `KEY` JSON path per item) has
no bit layout: its buffer is the JSON document itself, so that's what is
published, logged and decoded. The interface handles these too. It validates
every item exactly as for a binary packet, applies write conversions and
states, and writes the values into the document (parsed and serialised once
per packet). See `JSONDEMO STATUSJ` / `WAVEJ`.

- Unsent items are **absent** (`DEFAULTS`, starting from the definition's
  `TEMPLATE` or `{}`), so COSMOS reads them as nil rather than 0. `LAST` starts
  from the last document received.
- A derived item or conversion that reads a missing item makes COSMOS decom
  raise and drop the whole packet. Send those items, or make the conversion
  handle nil.
- Logged packets are 2–3× larger than packed binary, and build plus decom cost
  about 1.5× more per packet (`./test/run_bench.sh build`).
- `GET /tlm/<TGT>/<PKT>` reports `"accessor"` and each item's `"key"`.

The demo also installs `JSONHTTP`, the same packets fed by COSMOS's built-in
`HttpServerInterface` on port `json_http_demo_port` (7781), purely for
comparison. That interface has no authentication and doesn't validate bodies
(it replies 200, then decom drops bad packets), so it's only reachable inside
the COSMOS network. `./test/compare_paths.sh` benchmarks every path end to end.

## Authentication modes

Pick with the `json_tlm_auth_mode` plugin variable. `GET /health` is always open;
everything else requires auth.

### TOKEN mode — shared secret (COSMOS Core, default)

Senders present a static token as either header:

```
Authorization: Bearer <token>
X-Api-Key: <token>
```

Three ways to supply the expected token to the interface, best first:

| How | plugin.txt | Notes |
|---|---|---|
| **Secret** (recommended) | `SECRET ENV JSON_TLM_TOKEN JSON_TLM_AUTH_TOKEN AUTH_TOKEN` | Value lives in Admin > Secrets, never in config. This is what the default `json_tlm_auth_secret` variable wires up. |
| Inline option | `OPTION AUTH_TOKEN my-token` | Token sits in plugin.txt — avoid outside local use. |
| None | `OPTION ALLOW_UNAUTHENTICATED TRUE` | No auth at all. Local testing only. |

Setup: create the secret named by `json_tlm_auth_secret` (default
`JSON_TLM_TOKEN`) in **Admin > Secrets**, then install with
`json_tlm_auth_mode TOKEN`.

### OIDC mode — Keycloak JWTs (COSMOS Enterprise)

No shared secret. The sending service obtains a short-lived JWT from Keycloak and
presents it as `Authorization: Bearer <jwt>`. The interface acts as an OAuth2
resource server: it verifies the **RS256 signature** against the realm's JWKS
(fetched once, cached, refreshed automatically on key rotation), checks
`iss` / `aud` / `exp` / `nbf`, and then requires a **realm role**.

Setup:
1. Install with `json_tlm_auth_mode OIDC` and `json_tlm_oidc_role tlm_writer`
   (leaving the role blank accepts any valid realm token — not recommended).
2. In **Keycloak**: create a client for the sender with the `client_credentials`
   grant, and assign it the `tlm_writer` role.
3. Nothing else — issuer, JWKS URL and audience default to the
   `OPENC3_KEYCLOAK_URL`, `OPENC3_KEYCLOAK_REALM` and `OPENC3_API_CLIENT`
   environment variables COSMOS Enterprise already injects.

Getting and using a token:

```bash
TOKEN=$(curl -s -X POST \
  "$OPENC3_KEYCLOAK_URL/realms/openc3/protocol/openid-connect/token" \
  -d grant_type=client_credentials \
  -d client_id=telemetry-source -d client_secret=$SECRET | jq -r .access_token)

curl -X POST https://cosmos.example/json-tlm/tlm/INST/HEALTH_STATUS \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"items": {"TEMP1": 12000}}'
```

**Do I need COSMOS permissions on top of the role?** No. Posted telemetry enters
through the interface and never transits the cmd-tlm-api, where COSMOS's RBAC
lives — so the realm role checked here *is* the authorization boundary. You only
create the Keycloak client and assign it the role.

OIDC mode requires `PyJWT[crypto]`, which ships in `requirements.txt`; COSMOS
installs it into the interface's Python venv when the plugin loads.

## Configuration reference

### Plugin variables (install screen)

| Variable | Default | Meaning |
|---|---|---|
| `json_tlm_enabled` | `true` | `false` removes the interface entirely |
| `json_tlm_targets` | `JSONDEMO` | Space-separated existing targets to accept JSON for |
| `json_tlm_mapping` | `ENABLED` | Initial per-target state (`ENABLED`/`DISABLED`, flip later on the Interfaces tab) |
| `include_demo_target` | `true` | Install the `JSONDEMO` test target, and the `JSONHTTP` comparison target and interface |
| `json_http_demo_port` | `7781` | Port of the comparison `HttpServerInterface` (internal only, no auth) |
| `json_tlm_int_name` | `JSON_TLM_INT` | Interface name |
| `json_tlm_port` | `7780` | Port the HTTP server binds inside the container |
| `json_tlm_route_prefix` | `/json-tlm` | Traefik path prefix (the public URL) |
| `json_tlm_default_type` | `RAW` | Value type when a body omits `type` |
| `json_tlm_missing_items` | `DEFAULTS` | `DEFAULTS` or `LAST` for unsent items |
| `json_tlm_empty_variable_items` | `CHECK` | Guard for empty `VARIABLE_BIT_SIZE` items: `CHECK`, `REJECT` or `ALLOW` (see gotchas) |
| `json_tlm_auth_mode` | `TOKEN` | `TOKEN` or `OIDC` |
| `json_tlm_auth_secret` | `JSON_TLM_TOKEN` | (TOKEN) Admin > Secrets entry holding the token |
| `json_tlm_oidc_role` | `tlm_writer` | (OIDC) realm role a token must carry; blank = any valid token |

### Interface OPTIONs (what the variables expand to)

| Option | Mode | Default | Meaning |
|---|---|---|---|
| `AUTH_MODE` | both | `TOKEN` | `TOKEN` or `OIDC` |
| `AUTH_TOKEN` | TOKEN | — | Expected token (usually set via `SECRET`, not inline) |
| `ALLOW_UNAUTHENTICATED` | TOKEN | `FALSE` | `TRUE` disables auth (local only) |
| `OIDC_REQUIRED_ROLE` | OIDC | — | Realm role required to post |
| `OIDC_REQUIRED_SCOPE` | OIDC | — | OAuth scope required to post |
| `OIDC_AUDIENCE` | OIDC | `OPENC3_API_CLIENT` (`api`) | Expected `aud` |
| `OIDC_ISSUER` | OIDC | `OPENC3_KEYCLOAK_URL`/realms/`OPENC3_KEYCLOAK_REALM` | Override issuer |
| `OIDC_JWKS_URL` | OIDC | `<issuer>/protocol/openid-connect/certs` | Override JWKS URL |
| `OIDC_LEEWAY` | OIDC | `30` | Allowed clock skew (seconds) for `exp`/`nbf` |
| `DEFAULT_TYPE` | both | `RAW` | `RAW` or `CONVERTED` when body omits `type` |
| `MISSING_ITEMS` | both | `DEFAULTS` | `DEFAULTS` (clean buffer) or `LAST` (keep previous) |
| `STRICT_CONVERTED` | both | `TRUE` | Reject CONVERTED writes that would double-convert |
| `EMPTY_VARIABLE_ITEMS` | both | `CHECK` | `CHECK`: `400` if COSMOS decom couldn't decode the packet because of an empty variable item. `REJECT`: `400` for any empty variable item. `ALLOW`: no guard |
| `LISTEN_ADDRESS` | both | `0.0.0.0` | Bind address inside the container |
| `MAX_BODY_BYTES` | both | `1048576` | Max request body; larger → `413` |
| `MAX_QUEUE` | both | `10000` | Backpressure limit; full → `503` |

## Using it on other transports (MQTT/TCP)

Same JSON → packet conversion, attached as a READ protocol to any interface
whose payload is a JSON document. The body must name `target` and `packet`:

```
INTERFACE MQTT_INT openc3/interfaces/mqtt_interface.py host 1883
  MAP_TLM_TARGET INST
  PROTOCOL READ json_packet_protocol.py RAW DEFAULTS TRUE
#                                        |   |        |
#                           DEFAULT_TYPE-+   |        +-STRICT_CONVERTED
#                                 MISSING_ITEMS
```

A message that fails to convert is logged and dropped (returns `STOP`) so one bad
payload never disconnects the interface.

## Behavior & gotchas

- **RAW is the default.** Match it to your definition; use `CONVERTED` to send
  engineering values and state names (see [type](#request-body-reference)).
- **Unsent items start clean.** With `MISSING_ITEMS DEFAULTS` (default), items you
  don't send are zero / the template value, with ID items set — so a packet can't
  carry stale fields from a previous one. `LAST` keeps the previous values
  (matching `inject_tlm`).
- **DERIVED items can't be set** — COSMOS computes them during decom. Sending one
  is a `400`.
- **Values must fit the binary field** — a 16-bit UINT rejects `70000`; a STRING's
  bytes must fit its defined length.
- **All errors in a packet are reported at once** — fix them in one round trip.
- **Empty variable items break COSMOS decom (COSMOS core bug).** When a
  `VARIABLE_BIT_SIZE` item has a size of 0, COSMOS's decom reads it as "the
  rest of the packet". For strings, blocks and 8-bit arrays that item shows the
  bytes after it. For **arrays of wider elements** (e.g. INT16), decom can raise
  `array_size … not a multiple of bit_size` and **drop the whole packet** from
  the current value table (it's still in the raw log). The bytes the interface
  sends are correct; this happens for any interface, not only JSON.
  **The interface guards against it** (`EMPTY_VARIABLE_ITEMS CHECK`, the
  default): a packet with an empty variable item is test-decoded the way COSMOS
  will, and if that fails the sender gets a `400` naming the item instead of the
  packet silently vanishing. Trade-off: a rejected packet isn't in the raw log
  either, whereas a dropped one is. `REJECT` refuses any empty variable item
  (even ones that decode, but read wrong); `ALLOW` turns the guard off. Until
  core is fixed, the real cure is to send every variable item with at least one
  element, or use `MISSING_ITEMS LAST` so unsent ones keep their previous value.
- **Variable size writes are fixed up in the plugin.** COSMOS's write path sizes
  variable items as `len(value) × 8`, which is wrong for arrays with elements
  that aren't 8 bits and for non-ASCII strings. The interface swaps in its own
  `BinaryAccessor` subclass on its private copy of the packet to size them
  correctly. Packets with a non-binary accessor (JSON, CBOR, …) are left alone.
- **Python only.** This is the Python interface; targets whose conversions are
  Ruby classes need a Ruby port.

## Throughput & concurrency

The interface is a single `ThreadingHTTPServer` in one Python process. All request
work — JSON parse, building the packet from the definition, running conversions,
validating — is CPU-bound Python, so it's bounded by **one core / the GIL**. The
`202` is returned once the packet is queued; decommutation drains the queue
asynchronously.

Live numbers from `./test/compare_live.sh` on a local COSMOS 7.4.1 (i7-1165G7,
clients inside the operator container, 5 s runs). "Sustained" counts until the
interface had published every accepted packet to Redis:

| Load | inject_tlm | JSON interface (sustained) |
|---|---|---|
| STATUS, 1 client | 311 pkt/s, 3.0 ms | **1,206 pkt/s**, 0.8 ms (3.9×) |
| STATUS, 4 clients | 570 pkt/s, 6.6 ms | **1,281 pkt/s**, 2.9 ms (2.2×) |
| STATUS, 16 clients | 475 pkt/s, 31 ms | 884 pkt/s, 17 ms (1.9×) |
| WAVE 4 KB, 1 client | 126 pkt/s, 7.6 ms | **647 pkt/s**, 1.4 ms (5.1×) |
| WAVE 4 KB, 4 clients | 192 pkt/s, 20 ms | 568 pkt/s, 6.6 ms (3.0×) |
| STATUS, batches of 100, 1 client | (no batch form) | **2,639 pkt/s** (accepted 4,076/s, some `503`s) |
| STATUS, batches of 100, 16 clients | (no batch form) | 1,360 pkt/s |

The live ceiling for one interface is about **2,600 packets/s**, set by publishing
each packet to Redis, not by HTTP: batches are *accepted* faster than that, the
queue fills, and the interface answers `503` until it catches up.

To measure without a running COSMOS (real COSMOS library, fakeredis, no Traefik
or Redis publish, so higher than live), run `./test/run_bench.sh build` for
per-packet costs and `./test/run_bench.sh http` for HTTP load. On an i7-1165G7:

| Packet (demo target) | Build µs | COSMOS decom µs | Single POST, 4 clients | Batch of 100 |
|---|---|---|---|---|
| `STATUS` (26 B, all items) | 59 | 90 | ~2,100 req/s | ~10,500 pkt/s |
| `WAVE` (4 KB array) | 440 | 370 | ~810 req/s | — |
| `VARIABLE` small (4 variable items) | 270 | 140 | ~1,100 req/s | ~2,500 pkt/s |
| `VARIABLE` large (8 KB, variable) | 760 | 470 | ~490 req/s | — |
| `TAIL` (1 KB dynamic string) | 31 | 55 | — | — |

To compare against COSMOS's built-in `inject_tlm` on a **running** COSMOS (plugin
installed with the demo target), run `./test/compare_live.sh`. It asks for your
COSMOS password and the plugin token, then prints the table above.

`VARIABLE_BIT_SIZE` packets cost more to build because every variable item
moves the bytes after it, and each one needs its own copy of the packet's items.

Guidance:

- **Keep client concurrency modest — about 4–16.** More threads don't add
  throughput (the GIL serializes the work); they just add latency, and a
  connection storm can exceed the server's small TCP backlog and surface as
  Traefik `502`s. Have senders retry `502`/`503` with backoff.
- **Batch whenever you can.** `POST /tlm` with many packets amortizes the HTTP
  round-trip and JSON parse: about 2× the sustained rate of single POSTs live.
- **Scale out with more interfaces, not more threads.** Install additional JSON
  interfaces on their own `json_tlm_port` / `json_tlm_route_prefix` (each is a
  separate process with its own GIL) and shard targets across them.
- Real throughput drops with heavier conversions, larger packets, and more
  targets. If the queue backs up to `MAX_QUEUE` (default 10,000) the interface
  returns `503` — that means publishing to Redis, not the HTTP layer, is the
  limit. Treat `503` as "retry shortly", not as an error.

## Errors & troubleshooting

| Status | Means | Fix |
|---|---|---|
| `202` | Accepted and queued | — |
| `400` | Bad JSON, unknown item, bad value/range, DERIVED item, or empty `items` | Read the `error` message (lists every problem); check `GET /tlm/<TGT>/<PKT>` |
| `401` | Missing/invalid token, or (OIDC) valid token lacking the required role/scope | Send a valid `Authorization: Bearer …`; check the role in Keycloak |
| `403` | Target isn't mapped to this interface | Add it to `json_tlm_targets` |
| `404` | Unknown packet, or wrong path | Check target/packet names and the route prefix |
| `409` | Target is mapped but DISABLED | Enable it on the CmdTlmServer > Interfaces tab |
| `413` | Body larger than `MAX_BODY_BYTES` | Split the batch or raise the limit |
| `503` | Queue full (backpressure) or interface not connected | Retry with backoff |

Responses are always `{"error": "..."}` on failure, `{"accepted": N}` on success.

**Interface won't connect / keeps reconnecting**
- TOKEN mode: the secret named by `json_tlm_auth_secret` doesn't exist — create it
  in Admin > Secrets. (Without a token and without `ALLOW_UNAUTHENTICATED`, the
  interface refuses to start by design.)
- OIDC mode: `PyJWT` missing (check `requirements.txt` installed), or
  `OPENC3_KEYCLOAK_URL` unset and no `OIDC_ISSUER` given.

Check the interface state and counters on **CmdTlmServer > Interfaces**, or
`GET /health`, or the interface details (includes `auth_mode` and, in OIDC mode,
the resolved issuer/audience/required role).

## Running the tests

Tests run against the COSMOS Python library using its fakeredis harness — no
running COSMOS required. The helper script locates the venv, sets
`COSMOS_PLUGIN_DIR`, and handles the NixOS `libstdc++` loader quirk:

```bash
./test/run_tests.sh              # everything
./test/run_tests.sh -k oidc      # extra args pass through to pytest
```

Or directly:

```bash
cd cosmos/openc3/python
COSMOS_PLUGIN_DIR=/path/to/openc3-cosmos-json-tlm python -m pytest $COSMOS_PLUGIN_DIR/test -v
```

The OIDC authorization tests (roles, scopes, mode routing, 401s) stub the crypto
backend and always run. The real RS256 signature tests (`TestOidcRealCrypto`)
auto-skip unless `PyJWT[crypto]` is installed (`pip install 'PyJWT[crypto]'`);
they run automatically inside COSMOS, where `requirements.txt` provides it.
```
