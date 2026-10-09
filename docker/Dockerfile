# Headless PX4 SITL + Gazebo Harmonic + the Lost in Place runner (x86-64).
# Build from the repo root:  docker build -f docker/Dockerfile -t lost-in-place .
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Gazebo Harmonic from the OSRF repository; OpenCV is needed by PX4's optical-flow plugin.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg lsb-release sudo \
 && curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] http://packages.osrfoundation.org/gazebo/ubuntu-stable noble main" \
      > /etc/apt/sources.list.d/gazebo-stable.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gz-harmonic libopencv-dev \
 && rm -rf /var/lib/apt/lists/*

# PX4 at the pinned commit, with our patches, built for SITL.
COPY px4/ /opt/lost-in-place/px4/
RUN git clone --quiet https://github.com/PX4/PX4-Autopilot.git /opt/PX4-Autopilot \
 && /opt/lost-in-place/px4/apply.sh /opt/PX4-Autopilot \
 && bash /opt/PX4-Autopilot/Tools/setup/ubuntu.sh --no-nuttx --no-sim-tools \
 && rm -rf /var/lib/apt/lists/*
# The default c++ may be clang without libstdc++ headers; PX4 SITL builds cleanly with gcc.
RUN cd /opt/PX4-Autopilot && CC=gcc CXX=g++ make px4_sitl

# The runner.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /opt/lost-in-place
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY scenarios/ scenarios/
RUN uv sync --frozen --no-dev --extra aws

ENV LIP_PX4_DIR=/opt/PX4-Autopilot \
    LIP_WORLDS_DIR=/opt/worlds \
    PATH=/opt/lost-in-place/.venv/bin:$PATH
RUN lip worlds

RUN useradd --create-home lip
USER lip
ENTRYPOINT ["lip"]
CMD ["doctor"]
