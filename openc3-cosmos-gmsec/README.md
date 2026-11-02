# OpenC3 COSMOS GMSEC Telemetry Plugin

Subscribes to GMSEC subjects over **ActiveMQ** with the **GMSEC API** Python3
binding, **4.9.1 or 5.x** (the interface detects which one it is running against). Each message carries a raw packet in a binary field (`DATA`
in COMPAT C2 / C2MS `MSG.TLM` messages). The plugin passes those bytes into
the normal COSMOS telemetry pipeline, where your packet definitions decode
them. Nothing GMSEC-specific goes in your target definitions.

```
ActiveMQ ──> GmsecInterface ──> raw bytes from DATA ──> identify ──> decom ──> CVT / logs / screens
             (subscribe,        (+ subject and header    (ID items, or
              filter)            fields as packet extra)  META GMSEC_SUBJECT)
```

Contents:

- [Quick start](#quick-start): build the image, install the plugin, see telemetry
- [Adapting to your real messages](#adapting-to-your-real-messages): what to change once you know the message shape
- [Troubleshooting](#troubleshooting)
- [Reference](#reference): variables, options, image details, tests
- [Configuration examples](docs/examples.md): several targets, subject routing, headers, packed packets, SSL, scaling
- [How COSMOS handles telemetry](docs/telemetry-pipeline.md): the path from GMSEC message to screens, logs and database

---

## Quick start

You need Docker and access to the COSMOS images for your version. On
Kubernetes you also need a registry the cluster can pull from. Every command
below runs from the repo root.

### 1. Build the GMSEC image

GMSEC is not on PyPI, so it is compiled into a copy of the COSMOS operator
image. The build takes about 5 minutes. Each image holds one GMSEC version,
picked with `GMSEC_VERSION` (default `4.9.1`; `5.3` for GMSEC 5). Put both
versions in the tag; both Debian (7.4.x) and Alpine (7.2.x) bases work.

```bash
export COSMOS_VERSION=7.4.1                       # your COSMOS version
export GMSEC_VERSION=4.9.1                        # or 5.3
export GMSEC_IMAGE=<registry>/openc3-operator-gmsec:$COSMOS_VERSION-gmsec$GMSEC_VERSION

docker build -t $GMSEC_IMAGE --build-arg OPENC3_TAG=$COSMOS_VERSION \
  --build-arg GMSEC_VERSION=$GMSEC_VERSION openc3-cosmos-gmsec/docker
```

The plugin gem is the same for both; only the image differs. See
[GMSEC 4 and 5](#gmsec-4-and-5) for running both.

If you pull COSMOS from a mirror, add `--build-arg OPENC3_REGISTRY=<mirror>`
(and `OPENC3_NAMESPACE` if it isn't `openc3inc`). See
[Image details](#image-details) for air-gapped builds.

### 2. Check the image against a real broker

This starts a throwaway ActiveMQ, publishes demo telemetry with real GMSEC,
and decodes it with the plugin's interface running inside your image. It
needs no COSMOS install.

```bash
./openc3-cosmos-gmsec/test/e2e/run_activemq_e2e.sh $GMSEC_IMAGE
```

It should end with:

```
SEEN ['EVENT', 'HEALTH', 'POWER'] CONNECTED False
PASSED
```

### 3. Publish the image where COSMOS runs

```bash
docker push $GMSEC_IMAGE
```

- **Kubernetes (COSMOS Enterprise):** nothing else to do here. Step 5 tells
  COSMOS to run the GMSEC interface in this image. Make sure the cluster can
  pull it (`imagePullSecrets` for a private registry) and that pods can reach
  the broker.
- **Docker Compose (COSMOS Core):** Core runs every microservice inside the
  operator container, so swap the operator image in `compose.override.yaml`,
  then restart COSMOS:

  ```yaml
  services:
    openc3-operator:
      image: <registry>/openc3-operator-gmsec:7.4.1-gmsec4.9.1   # the $GMSEC_IMAGE you built
  ```

### 4. Build the plugin gem

```bash
cd openc3-cosmos-gmsec
VERSION=0.1.0 rake build
```

Without Ruby on the host, use a COSMOS image to build it:

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e VERSION=0.1.0 \
  -v "$PWD:/openc3/local:z" -w /openc3/local \
  docker.io/openc3inc/openc3-cosmos-cmd-tlm-api:$COSMOS_VERSION gem build openc3-cosmos-gmsec
```

This produces `openc3-cosmos-gmsec-0.1.0.gem`. Bump `VERSION` for every
upgrade.

### 5. Install the plugin

In COSMOS, go to **Admin → Plugins**, upload the gem, and set the variables:

| Variable | First install (demo) | Real use |
|---|---|---|
| `gmsec_targets` | `GMSECDEMO` | your target(s), space separated |
| `gmsec_mw_id` | `activemq39` | `activemq39` |
| `gmsec_server` | your broker, e.g. `tcp://activemq:61616` | e.g. `tcp://activemq.<ns>.svc:61616`, `failover:(tcp://a:61616,tcp://b:61616)`, `ssl://host:61617` |
| `gmsec_subjects` | `GMSEC.DEMO.SAT1.MSG.TLM.>` | your subjects (wildcards ok) |
| `gmsec_data_field` | `DATA` | the field holding the bytes, or `AUTO` |
| `gmsec_container` | Kubernetes: `$GMSEC_IMAGE`. Compose: blank | same |
| `include_demo_target` | `true` | `false` |

Install your own target plugin first, or alongside, so its packet definitions
exist. If the broker needs a login, see [Credentials](#credentials).

### 6. Check it in COSMOS

1. **CmdTlmServer → Interfaces:** `GMSEC_INT` should be CONNECTED. Its RX
   bytes and packets should climb while data is flowing.
2. **Packet Viewer or Telemetry Viewer:** your packets should update.
3. To see data without real traffic, publish the demo telemetry onto the
   broker COSMOS uses:

   ```bash
   docker run --rm --network <network that reaches the broker> \
     -v $PWD/openc3-cosmos-gmsec/tools:/tools:ro \
     -e PYTHONPATH=/opt/GMSEC_API/bin:/opt/GMSEC_API/bin/lib/GMSECAPI4:/opt/GMSEC_API/bin/lib/GMSECAPI5 \
     --entrypoint /openc3/python/.venv/bin/python $GMSEC_IMAGE \
     /tools/publish_demo_tlm.py mw-id=activemq39 server=tcp://activemq:61616 rate=2
   ```

   This publishes `GMSECDEMO HEALTH` and `POWER` (CCSDS, identified by APID)
   on `GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT`. Every 10th cycle it also sends an
   `EVENT` on `GMSEC.DEMO.SAT1.MSG.TLM.EVENT`, identified by subject.

If something doesn't look right, see [Troubleshooting](#troubleshooting).

---

## Adapting to your real messages

The demo assumes C2MS-style TLM messages with the packet in a BIN field named
`DATA`. The COMPAT C2 templates aren't public, so check this against your real
traffic before relying on it.

### 1. Look at real messages

`tools/dump_messages.py` subscribes and prints each message's fields, plus the
length and first bytes of every binary field. Run it from the GMSEC image on a
network that reaches your broker:

```bash
docker run --rm --network <network that reaches the broker> \
  -v $PWD/openc3-cosmos-gmsec/tools:/tools:ro \
  -e PYTHONPATH=/opt/GMSEC_API/bin:/opt/GMSEC_API/bin/lib/GMSECAPI4:/opt/GMSEC_API/bin/lib/GMSECAPI5 \
  --entrypoint /openc3/python/.venv/bin/python $GMSEC_IMAGE \
  /tools/dump_messages.py mw-id=activemq39 server=tcp://<broker>:61616 subject='GMSEC.>' count=5
```

Example output:

```
===== message 1: GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT =====
<MESSAGE SUBJECT="GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT" KIND="PUBLISH">
	<FIELD NAME="COLLECTION-POINT" TYPE="STRING">DEMO</FIELD>
	<FIELD NAME="DATA" TYPE="BIN">0064C000000407D0FF0600</FIELD>
	<FIELD NAME="MESSAGE-SUBTYPE" TYPE="STRING">TLM</FIELD>
	<FIELD NAME="STREAM-MODE" TYPE="STRING">RT</FIELD>
	...
</MESSAGE>
  BIN DATA: 11 bytes, first 11: 00 64 c0 00 00 04 07 d0 ff 06 00
```

Write down:

- [ ] the subject(s) telemetry arrives on
- [ ] which field holds the packet bytes, and its `TYPE` (BIN or something else)
- [ ] what the bytes are: exactly one packet, a header followed by a packet, or several packets
- [ ] how packets are told apart: an ID inside the bytes (e.g. CCSDS APID), the subject, or a message field
- [ ] header fields worth keeping (stream mode, collection point, time, ...)

### 2. Configure for what you found

Most cases need only install variables or an `OPTION` line in `plugin.txt`:

| What you find | What to change |
|---|---|
| Bytes are in a BIN field not named `DATA` | Set `gmsec_data_field` to that name |
| The field name varies, or there's only one BIN field | Set `gmsec_data_field` to `AUTO` |
| Different subjects from the demo | Set `gmsec_subjects` |
| Non-telemetry messages share the subjects | `OPTION FILTER <field> <value>`, e.g. `FILTER MESSAGE-SUBTYPE TLM`. Repeatable; all must match |
| Only real-time data wanted | `OPTION FILTER STREAM-MODE RT` |
| Header fields should travel with each packet | `OPTION EXTRA_FIELD <name>` for each field |
| Packets carry an ID in their bytes (e.g. APID) | Use `ID_ITEM` / `APPEND_ID_ITEM` in your packet definitions. Nothing to change in the plugin |
| One subject per packet type, with no ID in the bytes | Add `META GMSEC_SUBJECT <exact subject>` to each packet definition |
| An extra header sits in front of the packet | Add a COSMOS `PROTOCOL READ ...` to the interface in `plugin.txt` to strip it, or include the header in your packet definition |
| Several packets are packed into one message | Add a length or CCSDS `PROTOCOL READ ...` so COSMOS splits them |
| Broker needs a login | See [Credentials](#credentials) |
| SSL, failover or other GMSEC settings | `gmsec_server` (`ssl://`, `failover:(...)`), plus `OPTION CONFIG <key> <value>` |

These cases need a small code change in `lib/gmsec_interface.py`:

| What you find | Where to change it |
|---|---|
| Bytes stored as text (hex or base64 in a STRING field) | `find_data_field` / `extract`: decode the text into bytes |
| The message's own time should be the COSMOS received time | `convert_data_to_packet`: parse the field and set `packet.received_time` |
| Packets identified by a message field (e.g. `PKT-ID`) | The subject lookup in `__init__` and `convert_data_to_packet`: key it on that field |

### 3. Write your packet definitions

Definitions describe the raw bytes exactly as received: byte order, sizes, ID
items, conversions, limits. `targets/GMSECDEMO/cmd_tlm/tlm.txt` is a CCSDS
example to copy from. Set `include_demo_target` to `false` for real use.

### 4. Verify with a real sample before deploying

1. **Unit test.** Add a test to `test/test_gmsec_interface.py` that builds a
   message with your real field layout and sample bytes; the `tlm_message()`
   helper makes this a few lines. Run `./openc3-cosmos-gmsec/test/run_tests.sh`.
   Copy a test from `TestEndToEnd` to check the decoded values land in the
   current value table.
2. **Live test.** Edit `tools/publish_demo_tlm.py` to publish your real
   message shape (or replay captured bytes). Point `test/e2e/read_tlm.py` at
   your target, then run `./openc3-cosmos-gmsec/test/e2e/run_activemq_e2e.sh $GMSEC_IMAGE`.
3. **In COSMOS.** Rebuild the gem with a new `VERSION`, upgrade the plugin,
   and repeat [step 6](#6-check-it-in-cosmos) of the quick start.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Interface error: `GMSEC API Python3 binding (libgmsec_python3) not found` | The interface isn't running in the GMSEC image. Kubernetes: check `gmsec_container` is set and the pod pulled `$GMSEC_IMAGE`. Compose: check the operator image override and restart. |
| Interface never connects; GMSEC errors about the connection | Broker unreachable from the pod or container. Check `gmsec_server`, DNS, network policy and the port. Test from the image with `dump_messages.py`. |
| CONNECTED but RX stays at 0 | Subjects don't match, or every message is being skipped. Confirm subjects with `dump_messages.py`. Check `FILTER` options. Look in the interface log for `has no BIN field '<name>', skipped`; if it's there, fix `gmsec_data_field` or use `AUTO`. |
| RX climbs but packets show up as UNKNOWN | Identification doesn't match. Check the ID items against the bytes from `dump_messages.py`, or the `META GMSEC_SUBJECT` value against the exact subject, and that the target is listed in `gmsec_targets`. |
| Packets identified but values look wrong | The packet definition doesn't match the bytes: byte order, offsets, a header in front of the packet. Compare against the hex from `dump_messages.py`. |
| Interface process crashes (segfault) on the first message | The image was built without the binding fix. Rebuild from this Dockerfile; its smoke test checks for the fix. |
| Build fails at `make check_support build ...` | Run the build with `--progress=plain` and look for the first `error:`. The usual cause is a COSMOS base image this Dockerfile hasn't seen before. |

---

## Reference

### Packet identification

1. **ID items (default).** The bytes are identified like bytes from any other
   interface, for example by the CCSDS APID ID item.
2. **By subject.** A packet with `META GMSEC_SUBJECT <subject>` takes every
   message that arrives on that exact subject, with no ID items needed.

The packet's `extra` hash always holds `GMSEC_SUBJECT`. It also holds any
fields listed with `EXTRA_FIELD`; the plugin.txt default lists
`MESSAGE-SUBTYPE`, `COLLECTION-POINT` and `STREAM-MODE`.

### Install variables

| Variable | Default | Meaning |
|---|---|---|
| `gmsec_int_name` | `GMSEC_INT` | Interface name |
| `gmsec_targets` | `GMSECDEMO` | Targets whose telemetry arrives over GMSEC |
| `gmsec_mw_id` | `activemq39` | Middleware wrapper (`activemq39` and `activemq395` are the same library) |
| `gmsec_server` | `tcp://activemq:61616` | Broker URL |
| `gmsec_subjects` | `GMSEC.DEMO.SAT1.MSG.TLM.>` | Space separated subjects; wildcards allowed |
| `gmsec_data_field` | `DATA` | BIN field holding the packet. `AUTO` uses the first BIN field |
| `gmsec_api_home` | `/opt/GMSEC_API` | GMSEC install root inside the image |
| `gmsec_container` | blank | Enterprise only: image this interface runs in (emits `CONTAINER`) |
| `include_demo_target` | `true` | Installs the `GMSECDEMO` target |

### Interface options

```
INTERFACE GMSEC_INT gmsec_interface.py <mw-id> <server> <subject> [<subject> ...]
  OPTION DATA_FIELD DATA              # or AUTO
  OPTION CONFIG gmsec-schema-level 0  # any GMSEC connection config entry
  OPTION SUBSCRIBE GMSEC.OTHER.>      # more subjects
  OPTION FILTER MESSAGE-SUBTYPE TLM   # field must equal value (repeatable, all must match)
  OPTION FILTER STREAM-MODE RT
  OPTION EXTRA_FIELD COLLECTION-POINT # copy a field into packet extra (repeatable)
  OPTION RECEIVE_TIMEOUT 1.0          # receive poll period in seconds
  OPTION GMSEC_API_HOME /opt/GMSEC_API
  OPTION MW_USERNAME <user>           # broker credentials (see below)
  OPTION MW_PASSWORD <password>
```

The interface handles telemetry only. Sending commands raises an error.

### Credentials

Store the password in **Admin → Secrets** (e.g. as `GMSEC_PASSWORD`) and
reference it from the interface in `plugin.txt`, so it never appears in the
plugin:

```
  OPTION MW_USERNAME cosmos
  SECRET ENV GMSEC_PASSWORD GMSEC_PASSWORD MW_PASSWORD
```

### GMSEC 4 and 5

One gem, one image per GMSEC version. The interface checks which binding it
loaded (5.x renamed `ConnectionManager` to `Connection`) and adapts the few
calls that differ: connecting, releasing a received message, closing, and the
BIN field type constant. Nothing in `plugin.txt` or your targets changes.

| | GMSEC 4.9.1 | GMSEC 5.x |
|---|---|---|
| Build arg | `GMSEC_VERSION=4.9.1` (default) | `GMSEC_VERSION=5.3` |
| Binding directory | `bin/lib/GMSECAPI4` | `bin/lib/GMSECAPI5` |
| Core library | `libGMSECAPI.so` | `libgmsec_api.so` |
| ActiveMQ `mw-id` | `activemq39` / `activemq395` | same |

You usually need only one. The two versions interoperate on the wire: a 4.9.1
publisher fed a 5.3 interface, and a 5.3 publisher fed a 4.9.1 interface,
through the same ActiveMQ broker (`PUB_IMAGE=<4.9.1 image>
./test/e2e/run_activemq_e2e.sh <5.3 image>`). That was with the demo message
shape; check it with your real messages before relying on it. Throughput was
the same within run-to-run noise, measured in COSMOS 7.4.1:

| | Into current values, 1 / 4 publishers | Publish to interface output, idle median |
|---|---|---|
| 4.9.1 | 1,567 / 1,573 pkt/s | 2.1 ms |
| 5.3 | 1,502 / 1,453 pkt/s | 4.5 ms |

If you do run both, build both images and give each interface the one it
needs. On Kubernetes that is `gmsec_container` per install. On Docker Compose
every microservice shares the operator image, so one COSMOS runs one GMSEC
version.

The image reports its version in the `org.openc3.gmsec.version` label and the
`GMSEC_API_VERSION` environment variable.

### Image details

`docker/Dockerfile` starts from the stock `openc3-operator` image, builds
inside it, and copies only the results into a clean copy. The final image is
the operator image plus `/opt/GMSEC_API`:

- ActiveMQ-CPP 3.9.5, compiled from Apache source with SSL (OpenSSL 3)
- GMSEC API 4.9.1 or 5.x: the core library, the `activemq395` wrapper (plus
  its `activemq39` alias), the `mb` wrapper, the message templates, and the
  Python3 binding built against the image's own Python (3.13 on 7.4, 3.12 on 7.2)
- `GMSEC_API_HOME=/opt/GMSEC_API` and `LD_LIBRARY_PATH=/opt/GMSEC_API/bin`
- A fix for a GMSEC binding bug: `BinaryField.get_value()` calls the Python
  C API with the GIL released (SWIG `-threads`). On current Pythons that
  segfaults on the first received message, so the build marks those methods
  `nothread`. 5.x has the same bug in `BinaryField` and gets the same fix
- On Debian, an `/usr/include/apr-1` link in the build stage only: GMSEC 5's
  ActiveMQ wrapper takes a Linux without `/etc/lsb-release` for RHEL and
  looks for the APR headers there

Both COSMOS base image families work: 7.2.x is Alpine (musl) and 7.4.x is
Debian (glibc). The Dockerfile detects `apk` vs `apt`. GMSEC 4.9.1 was built
and passed the live ActiveMQ test on 7.4.1 and 7.2.0; GMSEC 5.3 so far only
on 7.4.1. Because the binding is tied to the image's Python, **rebuild
whenever you upgrade COSMOS**. A smoke test during the build imports the
binding, round-trips binary data and loads the ActiveMQ wrapper. The image is
x86_64 only. GMSEC 5 ships an aarch64 build configuration, but this
Dockerfile hasn't been tried on it.

GMSEC's own build compiles its libraries without optimization (no `-O` flag in
either version). The numbers above are with that default.

Build args: `OPENC3_REGISTRY`, `OPENC3_NAMESPACE`, `OPENC3_IMAGE` (default
`openc3-operator`), `OPENC3_TAG`, `GMSEC_VERSION` (default `4.9.1`),
`GMSEC_URL`, `ACTIVEMQ_CPP_URL`. For air-gapped builds, put
`GMSEC_API-<GMSEC_VERSION>.tar.gz` (GitHub tag tarball) and
`activemq-cpp-library-3.9.5-src.tar.gz` next to the Dockerfile and nothing is
downloaded.

On Kubernetes you can run every microservice in the GMSEC image instead of
just this interface: point the operator image in your Helm values or
manifests at it, and leave `gmsec_container` blank.

The plugin's `ENV` lines repeat the image defaults, so they also work when
GMSEC is mounted at a custom `gmsec_api_home`. On connect, the interface adds
the binding to `sys.path` and preloads the core library and the wrapper. If
GMSEC is missing, the interface shows a connection error that says so, and
COSMOS keeps retrying.

### Tests

| Test | Command | Needs |
|---|---|---|
| Unit (14 tests, run once per API) | `./openc3-cosmos-gmsec/test/run_tests.sh` | the repo's `openc3/python` venv; no GMSEC, broker or COSMOS |
| Live end-to-end | `./openc3-cosmos-gmsec/test/e2e/run_activemq_e2e.sh <image>` | Docker and a built GMSEC image |
| Live, mixed versions | `PUB_IMAGE=<other image> ./openc3-cosmos-gmsec/test/e2e/run_activemq_e2e.sh <image>` | Both images |

The unit tests use an in-process fake of `libgmsec_python3`
(`test/fake_gmsec`) and push messages through COSMOS's own `handle_packet` and
decom code into the current value table. The live test uses a real ActiveMQ
broker, the real binding and the real interface.

### Files

```
openc3-cosmos-gmsec/
├── lib/gmsec_interface.py        the COSMOS interface
├── plugin.txt                    install variables + INTERFACE definition
├── targets/GMSECDEMO/            demo packet definitions (CCSDS)
├── tools/publish_demo_tlm.py     publishes demo telemetry over GMSEC
├── tools/dump_messages.py        prints real messages to see their shape
├── docker/Dockerfile             builds the GMSEC-enabled operator image
└── test/
    ├── test_gmsec_interface.py   unit tests
    ├── fake_gmsec/               stand-in for libgmsec_python3
    └── e2e/                      live ActiveMQ test
```
