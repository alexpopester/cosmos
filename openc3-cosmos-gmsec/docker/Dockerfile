# A COSMOS image with the GMSEC API (C++ core, ActiveMQ wrapper and Python3
# binding) built in, for the openc3-cosmos-gmsec plugin. OPENC3_IMAGE picks the
# COSMOS image to add it to: openc3-operator (default), or openc3-base, the
# image the operator and API images are built from, to use as a base image.
# One GMSEC version per image, picked with GMSEC_VERSION (4.9.1 or a 5.x
# release); the plugin detects which one it is running against.
#
# Everything is compiled inside OPENC3_BUILD_IMAGE (openc3-base), so the Python
# binding matches the exact Python (and glibc/libstdc++) that openc3-base and
# every image built from it run. Only the build output lands in the final
# image. The compile doesn't depend on OPENC3_IMAGE, so building several images
# reuses it from the build cache (build_images.sh does that).
# Works on Debian (e.g. COSMOS 7.4.x) and Alpine/musl (e.g. 7.2.x) bases.
#
#   docker build -t <registry>/openc3-operator-gmsec:7.4.1-gmsec4.9.1 \
#     --build-arg OPENC3_TAG=7.4.1 --build-arg GMSEC_VERSION=4.9.1 openc3-cosmos-gmsec/docker
#
#   build_images.sh (next to this file) builds openc3-base and openc3-operator
#   in one go, tagged so other Dockerfiles can build FROM them.
#
# Air-gapped: put GMSEC_API-<GMSEC_VERSION>.tar.gz (GitHub tag tarball) and
# activemq-cpp-library-3.9.5-src.tar.gz next to this file and they are used
# instead of downloading.

ARG OPENC3_REGISTRY=docker.io
ARG OPENC3_NAMESPACE=openc3inc
ARG OPENC3_TAG=latest
ARG OPENC3_IMAGE=openc3-operator
ARG OPENC3_BUILD_IMAGE=openc3-base

FROM ${OPENC3_REGISTRY}/${OPENC3_NAMESPACE}/${OPENC3_BUILD_IMAGE}:${OPENC3_TAG} AS gmsec-build

ARG GMSEC_VERSION=4.9.1
ARG GMSEC_URL=https://github.com/nasa/GMSEC_API/archive/refs/tags/API-${GMSEC_VERSION}-release.tar.gz
ARG ACTIVEMQ_CPP_VERSION=3.9.5
ARG ACTIVEMQ_CPP_URL=https://archive.apache.org/dist/activemq/activemq-cpp/${ACTIVEMQ_CPP_VERSION}/activemq-cpp-library-${ACTIVEMQ_CPP_VERSION}-src.tar.gz

# COSMOS 7.4.x images are Debian (glibc), 7.2.x are Alpine (musl)
USER root
RUN if command -v apk >/dev/null; then \
      apk add --no-cache bash swig curl ca-certificates apr-dev util-linux-dev openssl-dev pkgconf \
        autoconf automake libtool linux-headers; \
    else \
      apt-get update && \
      apt-get install -y --no-install-recommends \
        swig curl ca-certificates libapr1-dev uuid-dev pkg-config && \
      rm -rf /var/lib/apt/lists/*; \
    fi && \
    # GMSEC 5's ActiveMQ wrapper treats a Linux without /etc/lsb-release as RHEL
    # and looks for APR in /usr/include/apr-1; Debian keeps it in apr-1.0
    if [ -d /usr/include/apr-1.0 ] && [ ! -e /usr/include/apr-1 ]; then ln -s apr-1.0 /usr/include/apr-1; fi

COPY . /context/
WORKDIR /build

# ActiveMQ-CPP 3.9.5 - the C++ client the GMSEC ActiveMQ wrapper is built on.
# It predates C++17 (dynamic exception specs, auto_ptr), so pin gnu++11. GMSEC
# expects it under GMSEC_SUPPORT/activemq395.
RUN src="/context/activemq-cpp-library-${ACTIVEMQ_CPP_VERSION}-src.tar.gz" && \
    if [ -f "$src" ]; then cp "$src" amq.tar.gz; else curl -fsSL -o amq.tar.gz "${ACTIVEMQ_CPP_URL}"; fi && \
    mkdir amq && tar -xzf amq.tar.gz -C amq --strip-components=1 && rm amq.tar.gz && \
    cd amq && \
    ./configure --prefix=/build/GMSEC_SUPPORT/activemq395 CXXFLAGS="-O2 -std=gnu++11" && \
    make -j"$(nproc)" && make install && \
    cd /build && rm -rf amq

# GMSEC's wrapper Makefile wants the CMS headers directly under include/ and
# APR under include/apr-1; ActiveMQ-CPP and Debian put them elsewhere
RUN cd /build/GMSEC_SUPPORT/activemq395/include && \
    ln -s "activemq-cpp-${ACTIVEMQ_CPP_VERSION}"/* . && \
    if [ -d /usr/include/apr-1.0 ]; then ln -s /usr/include/apr-1.0 apr-1; else ln -s /usr/include/apr-1 apr-1; fi

RUN src="/context/GMSEC_API-${GMSEC_VERSION}.tar.gz" && \
    if [ -f "$src" ]; then cp "$src" gmsec.tar.gz; else curl -fsSL -o gmsec.tar.gz "${GMSEC_URL}"; fi && \
    mkdir GMSEC_API && tar -xzf gmsec.tar.gz -C GMSEC_API --strip-components=1 && rm gmsec.tar.gz

# Binding bug fix: SWIG -threads releases the GIL around every wrapped call,
# including these hand-written %extend methods that call the Python C API
# (BinaryField.get_value builds a bytearray). Without the GIL that segfaults
# on Python 3.13, so keep the GIL held for them. The 4.x and 5.x bindings
# live in python3/gmsec4 and python3/gmsec5; 5.x has no ProductFile.i.
RUN cd "GMSEC_API/python3/gmsec${GMSEC_VERSION%%.*}/interfaces" && \
    for f in field/BinaryField.i mist/ProductFile.i; do \
      [ -f "$f" ] || continue; \
      sed -i 's/^%extend /%feature("nothread");\n%extend /' "$f" && \
      printf '\n%%feature("nothread", "0");\n' >> "$f"; \
    done

# python3-config is the image's system Python, which is also the interpreter
# behind /openc3/python/.venv. mb is always built; it needs no broker.
WORKDIR /build/GMSEC_API
RUN export GMSEC_PLATFORM=linux.x86_64 \
           SWIG_HOME=/usr \
           PYTHON3_HOME=/usr \
           MW_WRAPPERS=activemq395 && \
    make check_support build library mw_wrappers python3_api && \
    make install && \
    cp -a /build/GMSEC_SUPPORT/activemq395/lib/libactivemq-cpp.so* /build/release/GMSEC_API/bin/

# Smoke test: the binding imports under the venv Python, binary fields round
# trip (the GIL fix above), and the ActiveMQ wrapper loads (no broker needed)
RUN cd /build/release/GMSEC_API/bin && \
    LD_LIBRARY_PATH=. PYTHONPATH=".:lib/GMSECAPI${GMSEC_VERSION%%.*}" \
      /openc3/python/.venv/bin/python -X faulthandler /context/smoke_test.py


FROM ${OPENC3_REGISTRY}/${OPENC3_NAMESPACE}/${OPENC3_IMAGE}:${OPENC3_TAG}

USER root
RUN if command -v apk >/dev/null; then \
      apk add --no-cache apr libuuid; \
    else \
      apt-get update && \
      apt-get install -y --no-install-recommends libapr1t64 && \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY --from=gmsec-build /build/release/GMSEC_API/bin /opt/GMSEC_API/bin
COPY --from=gmsec-build /build/release/GMSEC_API/templates /opt/GMSEC_API/templates
COPY --from=gmsec-build /build/release/GMSEC_API/README.txt /opt/GMSEC_API/

# Same smoke test in the final image, in case OPENC3_IMAGE runs a different
# Python than OPENC3_BUILD_IMAGE
ARG GMSEC_VERSION=4.9.1
RUN --mount=type=bind,source=smoke_test.py,target=/tmp/gmsec_smoke_test.py \
    cd /opt/GMSEC_API/bin && \
    LD_LIBRARY_PATH=. PYTHONPATH=".:lib/GMSECAPI${GMSEC_VERSION%%.*}" \
      /openc3/python/.venv/bin/python -X faulthandler /tmp/gmsec_smoke_test.py

# Every microservice this image runs can find GMSEC, with or without the
# plugin's ENV lines
LABEL org.openc3.gmsec.version="${GMSEC_VERSION}"
ENV GMSEC_API_VERSION=${GMSEC_VERSION}
ENV GMSEC_API_HOME=/opt/GMSEC_API
ENV LD_LIBRARY_PATH=/opt/GMSEC_API/bin

USER ${USER_ID}:${GROUP_ID}
