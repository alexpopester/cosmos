# How COSMOS handles telemetry, and where GMSEC fits

This walks one telemetry packet through COSMOS, from the GMSEC message to the
screens, logs and database. It describes COSMOS Core 7.4.x as built from this
repo. File paths are relative to the repo root; most code exists in both Ruby
(`openc3/lib/openc3/`) and Python (`openc3/python/openc3/`), and the Python
path is given here because this interface is Python.

```
 GMSEC bus (ActiveMQ)
        │  message: subject + header fields + DATA (raw packet bytes)
        ▼
 ┌─────────────────────────── interface microservice ────────────────────────────┐
 │ GmsecInterface.read_interface  →  read protocols  →  convert_data_to_packet   │
 │ identify (ID items or META GMSEC_SUBJECT)  →  count  →  write raw packet      │
 └──────────────────────────────────────┬────────────────────────────────────────┘
                                        ▼
            Redis stream  DEFAULT__TELEMETRY__{TARGET}__PACKET   (raw bytes)
               │                    │                          │
               ▼                    ▼                          ▼
      PACKETLOG microservice   DECOM microservice         routers (optional)
      raw .bin log files       processors → limits →      forward raw packets
      in the logs bucket       decom → JSON                to other systems
                                    │
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                      ▼
   Redis stream DECOM__…    current value table      limits events topic
   (decoded JSON, ~1 min)   DEFAULT__tlm__TARGET     (limits monitor,
              │             (latest value of every    triggers, notifications)
              ▼              item: tlm(), screens)
      TSDB microservice
      QuestDB table per packet
      (history, graphs, data extractor)
```

## 1. Definitions come first

A plugin's `targets/<TARGET>/cmd_tlm/*.txt` files define every packet: its
items, their bit offsets and types, conversions, states, limits and the ID
items that identify it. On install, COSMOS parses them and stores the result
in Redis (`DEFAULT__openc3tlm__<TARGET>`). Every microservice below loads the
same definitions, so the interface, decom and the tools all agree on what a
packet's bytes mean.

Installing a target also starts that target's own microservices (see
`deploy_*_microservice` in `openc3/lib/openc3/models/target_model.rb`):

| Microservice | Reads | Writes |
|---|---|---|
| `DEFAULT__DECOM__<TARGET>` | raw `TELEMETRY` streams | `DECOM` streams, current value table, limits events |
| `DEFAULT__PACKETLOG__<TARGET>` | raw `TELEMETRY` streams | raw binary log files in the logs bucket |
| `DEFAULT__TSDB__<TARGET>` | `DECOM` streams | QuestDB, one table per packet |
| `DEFAULT__COMMANDLOG__<TARGET>`, `CLEANUP` | commands; old files | command logs; deletes expired logs |

`INTERFACE` and `ROUTER` lines start one more microservice each.

## 2. The interface microservice: bytes to an identified packet

`openc3/python/openc3/microservices/interface_microservice.py` runs one
interface. It connects it, then loops on `Interface.read()`
(`openc3/python/openc3/interfaces/interface.py`):

1. **`read_interface()`** returns one chunk of bytes and an `extra` dict. For
   this plugin, `GmsecInterface.read_interface` waits on `receive()`, applies
   `FILTER` options, takes the bytes from the `DATA` field and puts
   `GMSEC_SUBJECT` and any `EXTRA_FIELD`s in `extra`.
2. **Read protocols** (`PROTOCOL READ ...` in `plugin.txt`) can reshape the
   bytes: strip a header, split several packets out of one message, check a
   CRC, or drop a packet. With no protocols, one message is one packet.
3. **`convert_data_to_packet()`** wraps the bytes in a packet object. This
   plugin overrides it to return the packet named by `META GMSEC_SUBJECT`
   when the subject matches one.
4. **Identify.** For a packet not yet identified, `handle_packet` asks the
   definitions which packet these bytes are, among the targets mapped to this
   interface (`MAP_TLM_TARGET`), by comparing ID item values. No match makes
   it `UNKNOWN UNKNOWN` and logs the first bytes.
5. **Count and publish.** The packet's received count goes up, and
   `TelemetryTopic.write_packet` (`openc3/python/openc3/topics/telemetry_topic.py`)
   adds it to the Redis stream `DEFAULT__TELEMETRY__{TARGET}__PACKET`: received
   time, count, the raw buffer, and `extra` as JSON.

Nothing here decodes item values. The interface's only job is to get the
right bytes, identified, onto the stream as fast as possible.

## 3. Decom: raw bytes to values

`openc3/python/openc3/microservices/decom_microservice.py` reads the target's
`TELEMETRY` streams. For each packet, `decom_packet`:

1. Rebuilds the packet object from the stream entry (buffer, received time,
   count, `extra`).
2. Splits out subpackets, if the definition has any.
3. Runs **processors** (`PROCESSOR` in the definition), such as running
   averages, so their derived values exist before decom.
4. Checks **limits** against the current limits set. A change of limits state
   goes to the limits events topic, which feeds Limits Monitor, the
   notifications and any limits response code.
5. **Decoms** the packet. `TelemetryDecomTopic.write_packet`
   (`openc3/python/openc3/topics/telemetry_decom_topic.py`) reads every item
   as raw, converted, formatted and with its limits state into one JSON object.
   It then writes that JSON to the `DEFAULT__DECOM__{TARGET}__PACKET` stream
   and, for real-time packets, to the current value table.

Decom is where most CPU goes, so it sets how many packets per second a target
can take. One decom process per target handled about 1,600 small packets per
second in our tests. Above that, packets queue on the `TELEMETRY` stream
rather than being lost. `TARGET_MICROSERVICE DECOM` with `PACKET` lines in
`target.txt` splits a target's decom over several processes (untested with
this plugin).

## 4. Where the data ends up

| Store | What it holds | Who reads it |
|---|---|---|
| **Current value table** (Redis hash `DEFAULT__tlm__<TARGET>`, `openc3/python/openc3/models/cvt_model.py`) | The latest decoded packet, per packet | `tlm()` and the other script APIs, Telemetry Viewer screens, Packet Viewer |
| **`DECOM` streams** | Every decoded packet, kept about a minute | Real-time graphs and Data Extractor while live, the TSDB microservice |
| **QuestDB** (TSDB microservice, `openc3/python/openc3/microservices/tsdb_microservice.py`) | Every decoded packet. `extra` is kept in a `COSMOS_EXTRA` column | Historical graphs and Data Extractor. Kept for `TLM_DECOM_RETAIN_TIME` |
| **Raw log files** (PACKETLOG, logs bucket) | Every raw packet with its `extra`, in files cycled by `TLM_LOG_CYCLE_TIME` / `TLM_LOG_CYCLE_SIZE` | Replay, and download from Bucket Explorer. Kept for `TLM_LOG_RETAIN_TIME` |

When a tool asks for a time range, the streaming API
(`openc3-cosmos-cmd-tlm-api/app/models/logged_streaming_thread.rb`) reads
QuestDB for the past and switches to the Redis streams to catch up to live.

**Real-time and stored.** A packet marked `stored` (played back, not live) goes
through the same path. It still lands in the `DECOM` stream, QuestDB and the
logs, but it doesn't update the current value table. This plugin always marks
packets real-time. If your bus also carries playback (`STREAM-MODE` other than
`RT`), filter it out with `OPTION FILTER STREAM-MODE RT`, or it will overwrite
current values with old data.

## 5. Other ways telemetry gets in

All three paths below end in the same `TELEMETRY` stream, so decom, logging and
storage behave the same. They differ in what the client has to send.

| Path | What the client sends | Notes |
|---|---|---|
| **This plugin** (or any interface) | Raw packet bytes | Identification and decom use the definitions. Nothing is decoded before COSMOS. |
| **`inject_tlm`** (script API) | Item values | COSMOS starts from the packet's last buffer, writes the values in (`handle_inject_tlm` in `openc3/python/openc3/microservices/interface_decom_common.py`), then publishes the rebuilt bytes. One API call per packet. |
| **JSON TLM interface** (`json-tlm` plugin) | Item values or raw bytes over HTTP | An interface like this one, fed by HTTP instead of a bus |

The measured comparison of these paths is in the design doc ("COSMOS Telemetry
Ingest Paths").

## 6. Scaling and sharding

- **Per-target work.** Decom, logging and the TSDB writer run per target, so
  splitting a high-rate feed across targets spreads the load.
- **Per-packet work.** `TARGET_MICROSERVICE DECOM` with `PACKET` puts chosen
  packets in their own decom process.
- **`DB_SHARD`** (Enterprise) puts a target's streams, current values and
  QuestDB tables on a separate Redis and QuestDB.
- **The interface itself** is one process with one GMSEC connection. In our
  tests it kept up well past decom's rate, so it isn't the limit.

## 7. When the interface is down

ActiveMQ topics keep nothing for a subscriber that isn't connected, so by
default messages published while the interface is down are lost. With
`OPTION DURABLE <name>` the broker keeps them and delivers the backlog on
reconnect, and COSMOS processes it like any other burst: it is logged, stored
and decoded at decom's normal rate. See "Durable subscriptions" in the README.

## 8. What this plugin doesn't do yet

- **Commands.** Writing to the interface raises an error.
- **Message time.** COSMOS's received time is when the interface got the
  message, not a time field in it. See "Adapting to your real messages" in the
  README to change that.
