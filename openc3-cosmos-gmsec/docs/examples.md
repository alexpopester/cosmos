# Configuration examples

Each example shows the `plugin.txt` lines that change and, where it matters,
the packet definition. They build on the default `plugin.txt`; anything not
shown stays as it is. For what each option does, see the Reference section of
the [README](../README.md#reference). For how the packet travels through COSMOS
after the interface, see [telemetry-pipeline.md](telemetry-pipeline.md).

The unit tests cover examples 2–4, and example 1 with a single target; the live
tests cover example 11. The others use standard COSMOS protocols and GMSEC
settings that haven't been run against this plugin yet. Check them with a
captured message first (README, "Verify with a real sample before deploying").

- [1. Several targets on one interface](#1-several-targets-on-one-interface)
- [2. One subject per packet, no ID in the bytes](#2-one-subject-per-packet-no-id-in-the-bytes)
- [3. Keep GMSEC header fields with each packet](#3-keep-gmsec-header-fields-with-each-packet)
- [4. Live data only, drop playback](#4-live-data-only-drop-playback)
- [5. A fixed header in front of the packet](#5-a-fixed-header-in-front-of-the-packet)
- [6. Several CCSDS packets in one message](#6-several-ccsds-packets-in-one-message)
- [7. Two buses, or two spacecraft](#7-two-buses-or-two-spacecraft)
- [8. Broker login, SSL and failover](#8-broker-login-ssl-and-failover)
- [9. A packet rate above what one decom keeps up with](#9-a-packet-rate-above-what-one-decom-keeps-up-with)
- [10. Kubernetes: run only this interface in the GMSEC image](#10-kubernetes-run-only-this-interface-in-the-gmsec-image)
- [11. Keep messages while the interface is down](#11-keep-messages-while-the-interface-is-down)

---

## 1. Several targets on one interface

One subscription carries telemetry for a spacecraft and its instrument. Each
target's packets have their own APIDs.

Install variables:

```
gmsec_targets   SAT1 SAT1_INST
gmsec_subjects  GMSEC.MISSION.SAT1.MSG.TLM.>
```

That emits one `MAP_TLM_TARGET` per target. COSMOS identifies each message's
bytes against the packets of both targets, by ID item:

```
TELEMETRY SAT1_INST SCIENCE BIG_ENDIAN "Instrument science packet"
  APPEND_ITEM    CCSDSVER     3 UINT
  APPEND_ITEM    CCSDSTYPE    1 UINT
  APPEND_ITEM    CCSDSSHF     1 UINT
  APPEND_ID_ITEM CCSDSAPID   11 UINT 300 "Only APID 300 is this packet"
  ...
```

Two packets with the same ID values in different targets can't be told apart.
Give one of them `META GMSEC_SUBJECT` (example 2) or split the targets across
two interfaces (example 7).

## 2. One subject per packet, no ID in the bytes

The bytes carry no APID, but each packet type has its own subject. Subscribe
to all of them and name the subject in each definition:

```
gmsec_subjects  GMSEC.MISSION.SAT1.MSG.TLM.>
```

```
TELEMETRY SAT1 THERMAL BIG_ENDIAN "Thermal, one per message on its subject"
  META GMSEC_SUBJECT GMSEC.MISSION.SAT1.MSG.TLM.THERMAL
  APPEND_ITEM PANEL_TEMP 16 INT
  ...

TELEMETRY SAT1 ATTITUDE BIG_ENDIAN "Attitude"
  META GMSEC_SUBJECT GMSEC.MISSION.SAT1.MSG.TLM.ATTITUDE
  APPEND_ITEM Q1 32 FLOAT
  ...
```

The subject must match exactly; wildcards work only in the subscription. A
message on a subject no packet claims falls back to ID items, then `UNKNOWN`.
The demo target's `EVENT` packet works this way.

## 3. Keep GMSEC header fields with each packet

```
INTERFACE <%= gmsec_int_name %> gmsec_interface.py ...
  OPTION EXTRA_FIELD COLLECTION-POINT
  OPTION EXTRA_FIELD STREAM-MODE
  OPTION EXTRA_FIELD MISSION-ID
```

Each field becomes a key in the packet's `extra`, next to `GMSEC_SUBJECT`.
`extra` is saved in the raw log files and in QuestDB's `COSMOS_EXTRA` column,
so you can tell later which ground station or stream a packet came from. It
doesn't become a telemetry item: screens and `tlm()` don't show it.

## 4. Live data only, drop playback

The bus carries both live (`STREAM-MODE RT`) and played-back data on the same
subjects. Playback would overwrite current values with old data, so drop it:

```
  OPTION FILTER STREAM-MODE RT
```

`FILTER` compares the field's string value. Several `FILTER` lines must all
match. To keep playback too, run a second interface without the filter that
feeds a different target (example 7), so it never touches the live target's
current values.

## 5. A fixed header in front of the packet

`DATA` holds an 8-byte ground header and then the CCSDS packet. The burst
protocol returns each message as one chunk, and its first argument drops that
many bytes from the front:

```
INTERFACE <%= gmsec_int_name %> gmsec_interface.py ...
  PROTOCOL READ openc3/interfaces/protocols/burst_protocol.py 8
```

To keep the header's values, leave out the protocol and put the header items
at the start of the packet definition instead.

## 6. Several CCSDS packets in one message

`DATA` holds several CCSDS packets back to back. The length protocol splits
them using the CCSDS length field: bit offset 32, 16 bits, plus 7 bytes
(the 6-byte primary header plus one, since CCSDS stores length minus one):

```
INTERFACE <%= gmsec_int_name %> gmsec_interface.py ...
  PROTOCOL READ openc3/interfaces/protocols/length_protocol.py 32 16 7 1 BIG_ENDIAN
```

Every packet split out of a message gets that message's `extra`. If a message
ends partway through a packet, the protocol waits for the next message to
finish it, so a lost message misaligns everything after it. The protocol's
sync pattern argument helps it recover if your packets have one.

## 7. Two buses, or two spacecraft

Add a second `INTERFACE` to `plugin.txt`. Each needs a unique name and its
own targets:

```
INTERFACE GMSEC_SAT1 gmsec_interface.py activemq39 tcp://bus-a:61616 GMSEC.MISSION.SAT1.MSG.TLM.>
  MAP_TLM_TARGET SAT1
  ENV GMSEC_API_HOME /opt/GMSEC_API
  ENV LD_LIBRARY_PATH /opt/GMSEC_API/bin

INTERFACE GMSEC_SAT2 gmsec_interface.py activemq39 tcp://bus-b:61616 GMSEC.MISSION.SAT2.MSG.TLM.>
  MAP_TLM_TARGET SAT2
  ENV GMSEC_API_HOME /opt/GMSEC_API
  ENV LD_LIBRARY_PATH /opt/GMSEC_API/bin
```

Each interface is its own process with its own connection. Splitting by target
also splits decom, logging and storage, since COSMOS runs those per target.

## 8. Broker login, SSL and failover

```
gmsec_server  failover:(ssl://bus-a:61617,ssl://bus-b:61617)
```

```
INTERFACE <%= gmsec_int_name %> gmsec_interface.py ...
  OPTION MW_USERNAME cosmos
  SECRET ENV GMSEC_PASSWORD GMSEC_PASSWORD MW_PASSWORD
  OPTION CONFIG mw-truststore /opt/certs/truststore.pem
```

- Store the broker password in **Admin → Secrets** as `GMSEC_PASSWORD`; it
  never appears in the plugin.
- `mw-truststore` is a GMSEC ActiveMQ setting (see the GMSEC docs for the
  matching keystore settings). The file must exist inside the interface's
  image, so add it in your own Dockerfile layer or mount it. Its password can
  only be set with `OPTION CONFIG`, which leaves it in plain text in the
  plugin.
- With `failover:`, ActiveMQ reconnects to the next broker by itself. COSMOS
  doesn't see a disconnect, so messages sent during the switch can be lost.

## 9. A packet rate above what one decom keeps up with

One decom process handled about 1,600 small packets per second in our tests.
Above that, packets wait in Redis and current values fall behind. If one
target has a few high-rate packets, give them their own decom process in that
target's `target.txt`:

```
TARGET_MICROSERVICE DECOM
  PACKET ATTITUDE
TARGET_MICROSERVICE DECOM
  PACKET THERMAL
```

Packets not listed stay in the target's main decom process. If the rate is
spread over many packets, splitting the target (example 7) does the same job.

## 10. Kubernetes: run only this interface in the GMSEC image

Enterprise only. Leave the operator on its stock image and run just this
interface in the GMSEC image:

```
gmsec_container  registry.example.com/openc3-operator-gmsec:7.4.1-gmsec5.3
```

That adds `CONTAINER <image>` to the interface. The cluster must be able to
pull from that registry. On Docker Compose there's no per-interface image;
replace the operator image instead (README, quick start step 3).

## 11. Keep messages while the interface is down

Restarts and upgrades shouldn't lose telemetry. Give the interface a durable
subscription name:

```
gmsec_durable_name  cosmos-sat1
```

That adds `OPTION DURABLE cosmos-sat1`. The broker then holds every message
published while the interface is down and delivers them when it reconnects.
With two interfaces (example 7), give each its own name. Tested live on
GMSEC 4.9.1 and 5.3, including a broker restart; see "Durable subscriptions"
in the README for what to watch for.
