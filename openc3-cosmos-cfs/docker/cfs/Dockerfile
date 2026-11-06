# NASA core Flight System (cFS), built for Linux: the lab apps (CI_LAB takes commands on
# UDP 1234, TO_LAB sends telemetry to UDP 1235 once enabled) and the sample app.
# Follows the OpenC3 "COSMOS and NASA cFS" guide; pinned to the commit it was tested with.
FROM ubuntu:24.04 AS build
ARG CFS_COMMIT=0ba1faa
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/nasa/cFS.git /cFS && cd /cFS && git checkout ${CFS_COMMIT} \
    && git submodule update --init --recursive
WORKDIR /cFS
RUN cp cfe/cmake/Makefile.sample Makefile && cp -r cfe/cmake/sample_defs sample_defs \
    && make SIMULATION=native BUILDTYPE=debug OMIT_DEPRECATED=true prep \
    && make && make install

FROM ubuntu:24.04
COPY --from=build /cFS/build /cFS/build
# The message headers, for checking the COSMOS packet definitions against them
COPY --from=build /cFS/cfe/modules /cFS/src/cfe/modules
COPY --from=build /cFS/apps /cFS/src/apps
WORKDIR /cFS/build/exe/cpu1
ENTRYPOINT ["./core-cpu1"]
