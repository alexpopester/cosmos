# NASA cFS behind a contact gate

Real flight software as the "spacecraft" for testing contact automation: NASA's core Flight
System (cFS) runs in a container, and COSMOS reaches it only through a contact gate that passes
traffic while a ground station can see the satellite cFS "flies" (NOAA 19 over Fairbanks by
default, about ten passes a day). Step 4 of `openc3-cosmos-flowautomator/docs/REAL-DATA-TESTING.md`.

```bash
docker/run.sh build && docker/run.sh start     # cFS + gate next to the local COSMOS
../openc3.sh cli rake build VERSION=X.Y.Z      # then install the gem (CFS target)
docker/run.sh status                           # link up?, elevation, next pass
```

After each cFS start (or restart) telemetry must be switched on: `CFS TO_LAB_ENABLE`
(destination 127.0.0.1, the gate). Commands and telemetry only get through during passes.

Faults, on the gate's control port (127.0.0.1:8089):

```bash
curl -X POST localhost:8089/fault -d '{"mode": "closed", "seconds": 120}'  # early LOS / dead pass
curl -X POST localhost:8089/fault -d '{"mode": "open", "seconds": 600}'    # link regardless of geometry
curl -X POST localhost:8089/fault -d '{"mode": "auto", "drop": 0.3}'       # 30% packet loss in passes
curl -X POST localhost:8089/fault -d '{"mode": "window", "aos": T1, "los": T2}'  # a staged pass (Unix seconds)
```

A reboot is `CFS ES_RESTART` (hazardous) or `docker restart cfs`.

The CFS definitions were checked against the build's own headers: command header 8 bytes,
telemetry header 16 (with 4 bytes of padding), little-endian payloads.
