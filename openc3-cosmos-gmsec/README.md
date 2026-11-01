# OpenC3 COSMOS GMSEC Telemetry Plugin

Subscribes to GMSEC subjects with the **GMSEC API 4.9.1** Python3 binding
(`libgmsec_python3`). Each message carries a raw packet in a BIN field (`DATA`
in COMPAT C2 / C2MS `MSG.TLM` messages). The plugin passes those bytes into
the normal COSMOS telemetry pipeline, where your existing packet definitions
decode them. Nothing GMSEC-specific goes in your target definitions.

```
GMSEC bus ──> GmsecInterface ──> raw bytes from DATA ──> identify ──> decom ──> CVT / logs
              (subscribe)        (+ subject, header      (ID items, or
                                  fields as packet extra)  META GMSEC_SUBJECT)
```

## Packet identification

1. **ID items (default).** The bytes are identified like bytes from any other
   interface, for example by the CCSDS APID ID item.
2. **By subject.** A packet with `META GMSEC_SUBJECT <subject>` takes every
   message that arrives on that exact subject, with no ID items needed.

The packet's `extra` hash always holds `GMSEC_SUBJECT`. It also holds any
fields listed with `EXTRA_FIELD`; the plugin.txt default lists `MESSAGE-SUBTYPE`,
`COLLECTION-POINT` and `STREAM-MODE`.

## Install variables

| Variable | Default | Meaning |
|---|---|---|
| `gmsec_targets` | `GMSECDEMO` | Targets whose telemetry arrives over GMSEC |
| `gmsec_mw_id` | `activemq39` | Middleware wrapper (`activemq39` and `activemq395` are the same library) |
| `gmsec_server` | `tcp://activemq:61616` | Broker URL, e.g. `failover:(tcp://a:61616,tcp://b:61616)` or `ssl://host:61617` |
| `gmsec_subjects` | `GMSEC.DEMO.SAT1.MSG.TLM.>` | Space separated subjects; wildcards allowed |
| `gmsec_data_field` | `DATA` | BIN field holding the packet. `AUTO` uses the first BIN field |
| `gmsec_api_home` | `/opt/GMSEC_API` | GMSEC install root inside the image |
| `gmsec_container` | blank | Enterprise/Kubernetes only: image this interface runs in (see below) |
| `include_demo_target` | `true` | Installs the `GMSECDEMO` target |

## Interface options

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
```

Broker credentials can come from Admin > Secrets, so they never sit in plugin.txt:

```
  OPTION MW_USERNAME cosmos
  SECRET ENV GMSEC_PASSWORD GMSEC_PASSWORD MW_PASSWORD
```

The interface handles telemetry only. Sending commands raises an error.

## Making GMSEC available to COSMOS

GMSEC is not on PyPI, so the plugin cannot install it. The interface must run
in an image that has GMSEC built for that image's own Python. `docker/Dockerfile`
builds one. It starts from the stock `openc3-operator` image and adds:

- ActiveMQ-CPP 3.9.5, compiled from Apache source with SSL (OpenSSL 3)
- GMSEC API 4.9.1: `libGMSECAPI.so`, the `activemq395` wrapper (plus its
  `activemq39` alias), the `mb` wrapper, the message templates, and the
  Python3 binding built against the image's own Python (3.13 on 7.4, 3.12 on 7.2)
- `GMSEC_API_HOME=/opt/GMSEC_API` and `LD_LIBRARY_PATH=/opt/GMSEC_API/bin`
- A fix for a GMSEC binding bug: `BinaryField.get_value()` calls the Python
  C API with the GIL released (SWIG `-threads`). On current Pythons that
  segfaults on the first received message, so the build marks those methods
  `nothread`

Both COSMOS base image families work: 7.2.x is Alpine (musl) and 7.4.x is
Debian (glibc). The Dockerfile detects `apk` vs `apt`, so just pick
the base with `OPENC3_TAG`. Both were built and passed the live ActiveMQ test
on 7.4.1 and 7.2.0.

The build compiles inside the operator image itself, so glibc, libstdc++ and
Python match exactly. A smoke test then imports the binding and loads the
ActiveMQ wrapper. Rebuild whenever you move to a new COSMOS version.

```bash
docker build -t <registry>/openc3-operator-gmsec:7.4.1 \
  --build-arg OPENC3_TAG=7.4.1 openc3-cosmos-gmsec/docker
docker push <registry>/openc3-operator-gmsec:7.4.1
```

Build args: `OPENC3_REGISTRY`, `OPENC3_NAMESPACE`, `OPENC3_TAG` (match your
COSMOS version), `GMSEC_URL`, `ACTIVEMQ_CPP_URL`. For air-gapped builds, put
`GMSEC_API-4.9.1.tar.gz` and `activemq-cpp-library-3.9.5-src.tar.gz` next to
the Dockerfile.

### Kubernetes (COSMOS Enterprise)

Set the `gmsec_container` install variable to the pushed image. The plugin
then emits `CONTAINER <image>` for the interface, so the operator runs that
one microservice in the GMSEC image and everything else stays stock. The
image is built `FROM openc3-operator`, so it has the COSMOS Python
environment, entrypoint and user the microservice expects, and it mounts
`/gems` and the plugin venv like any other microservice.

Alternatively, run every microservice in the image: point your Helm values or
manifests' operator image at `openc3-operator-gmsec` and leave
`gmsec_container` blank.

The image must be pullable from the cluster (`imagePullSecrets` for a private
registry). The pod must also reach the broker, e.g. `tcp://activemq.<ns>.svc:61616`.

### Docker Compose (COSMOS Core)

Core ignores `CONTAINER` and runs every microservice inside the operator
container. Swap the operator image in `compose.override.yaml`:

```yaml
services:
  openc3-operator:
    image: <registry>/openc3-operator-gmsec:7.4.1
```

### Loading

The plugin's `ENV` lines repeat the image defaults, so they also work when
GMSEC is mounted at a custom `gmsec_api_home`. On connect, the interface adds
the binding to `sys.path` and preloads `libGMSECAPI.so` and the wrapper. If
GMSEC is missing, the interface shows a connection error that says so, and
COSMOS keeps retrying.

## Trying it out

1. Start an ActiveMQ broker the operator can reach, e.g.
   `docker run -d --name activemq --network <cosmos network> apache/activemq-classic:5.18.6`.
   Then set `gmsec_server` to `tcp://activemq:61616`.
2. Install the plugin with the defaults.
3. Publish demo telemetry from the GMSEC image:

   ```bash
   docker run --rm --network <cosmos network> -v $PWD/openc3-cosmos-gmsec/tools:/tools:ro \
     -e PYTHONPATH=/opt/GMSEC_API/bin:/opt/GMSEC_API/bin/lib/GMSECAPI4 \
     --entrypoint /openc3/python/.venv/bin/python <registry>/openc3-operator-gmsec:7.4.1 \
     /tools/publish_demo_tlm.py mw-id=activemq39 server=tcp://activemq:61616 rate=2
   ```

   This publishes CCSDS packets with APIDs 100 and 101 on
   `GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT`, which become `GMSECDEMO HEALTH` and
   `POWER` through ID items. Every 10th cycle it also publishes an `EVENT`
   packet on `GMSEC.DEMO.SAT1.MSG.TLM.EVENT`, identified by subject.

## Tests

```bash
./test/run_tests.sh
```

The tests run against the repo's COSMOS Python library and fakeredis harness,
with an in-process fake of `libgmsec_python3` (`test/fake_gmsec`). Messages go
through COSMOS's own `handle_packet` and decom code into the CVT. No GMSEC
install or broker is needed.

## Build

```bash
VERSION=0.0.1 rake build   # or: gem build openc3-cosmos-gmsec
```
