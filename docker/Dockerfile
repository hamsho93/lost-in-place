# Headless PX4 SITL + Gazebo Harmonic + the Lost in Place runner (x86-64).
# Build from the repo root:  docker build -f docker/Dockerfile -t lost-in-place .
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Gazebo Harmonic from the OSRF repository, plus the headless part of PX4's ubuntu.sh simulation
# dependencies (the Dockerfile skips them with --no-sim-tools). On 24.04 gz-transport's CMake target
# CPPZMQ::CPPZMQ needs cppzmq-dev; OpenCV is needed by PX4's optical-flow plugin.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg lsb-release sudo \
 && curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable noble main" \
      > /etc/apt/sources.list.d/gazebo-stable.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      gz-harmonic cppzmq-dev libunwind-dev libopencv-dev libeigen3-dev \
      bc libxml2-utils pkg-config protobuf-compiler \
 && rm -rf /var/lib/apt/lists/*

# PX4 at the pinned commit, with our patches, built for SITL.
COPY px4/ /opt/lost-in-place/px4/
RUN git clone --quiet https://github.com/PX4/PX4-Autopilot.git /opt/PX4-Autopilot \
 && /opt/lost-in-place/px4/apply.sh /opt/PX4-Autopilot \
 && bash /opt/PX4-Autopilot/Tools/setup/ubuntu.sh --no-nuttx --no-sim-tools \
 && rm -rf /var/lib/apt/lists/*
# The default c++ may be clang without libstdc++ headers; PX4 SITL builds cleanly with gcc.
RUN cd /opt/PX4-Autopilot && CC=gcc CXX=g++ make px4_sitl
# MAVLink stream rates for the offboard link, applied at boot (see the file).
COPY docker/4021_gz_x500_flow.post /opt/PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix/airframes/

# The runner.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /opt/lost-in-place
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY scenarios/ scenarios/
RUN uv sync --frozen --no-dev --extra aws

ARG GIT_SHA=unknown
ENV LIP_GIT_SHA=$GIT_SHA \
    LIP_PX4_DIR=/opt/PX4-Autopilot \
    LIP_WORLDS_DIR=/opt/worlds \
    PATH=/opt/lost-in-place/.venv/bin:$PATH
RUN lip worlds

# `lip run` writes episodes to ./runs; the rest of /opt/lost-in-place stays root-owned.
RUN useradd --create-home lip \
 && install -d -o lip -g lip /opt/lost-in-place/runs
USER lip
ENTRYPOINT ["lip"]
CMD ["doctor"]
