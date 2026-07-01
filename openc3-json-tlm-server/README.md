# openc3-json-tlm-server

Provides `JsonTelemetryServerInterface` — an HTTP server that accepts `POST /<TARGET>/<PACKET>` requests with a flat JSON body and routes them to COSMOS telemetry packets.

No HTTP-specific fields (`ACCESSOR`, `KEY`, `HTTP_PATH`) are required in packet definitions. Any existing target can be mapped to this interface without modification.

## Usage

Add to `plugin.txt`:

```
INTERFACE MY_INT json_telemetry_server_interface.rb 8765
  # SECRET_OPTION API_KEY <secret_name>   # optional
  OPTION LISTEN_ADDRESS 0.0.0.0            # optional
  MAP_TARGET MY_TARGET
```

Then POST telemetry:

```bash
curl -X POST http://localhost:8765/MY_TARGET/MY_PACKET \
  -H "Content-Type: application/json" \
  -d '{"TEMPERATURE": 25.5, "PRESSURE": 1013.2}'
```

## HTTP Contract

| Status | Meaning |
|--------|---------|
| 200 OK | Packet accepted |
| 400 Bad Request | JSON body could not be parsed |
| 401 Unauthorized | API key missing or wrong (when configured) |
| 404 Not Found | Unknown target or packet in URL path |
| 405 Method Not Allowed | Non-POST method used |

## Authentication

Configure via `SECRET_OPTION API_KEY <secret_name>` in `plugin.txt`. Send the token as:

```
Authorization: Bearer <token>
```

When no `API_KEY` is configured all requests are accepted.

## Loading

Build and load the gem:

```bash
cd openc3-cosmos-init/plugins/packages/openc3-json-tlm-server
gem build openc3-json-tlm-server.gemspec
./openc3.sh cli load openc3-json-tlm-server-*.gem
```
