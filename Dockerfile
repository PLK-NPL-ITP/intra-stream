FROM ubuntu:24.04 AS build

# https://serverfault.com/questions/949991/how-to-install-tzdata-on-a-ubuntu-docker-image
ENV DEBIAN_FRONTEND=noninteractive

# To use if in RUN, see https://github.com/moby/moby/issues/7281#issuecomment-389440503
SHELL ["/bin/bash", "-c"]

# Install all build dependencies including python3.12 and python3.12-venv for python_addons
# Also install dependencies needed for FFmpeg compilation from 3rdparty
RUN if [[ $INSTALLDEPENDS != 'NO' ]]; then \
        apt-get update && apt-get install -y \
            gcc make g++ patch unzip perl git libasan5 \
            python3.12 python3.12-venv python3.12-dev \
            autoconf automake libtool pkg-config \
            zlib1g-dev liblzma-dev libzip-dev libbz2-dev \
            tcl curl wget ca-certificates cmake \
            nasm yasm ffmpeg \
            && rm -rf /var/lib/apt/lists/*; \
    fi

# Create symlink for python3.12 to be available as python3.12 command
RUN if [[ $INSTALLDEPENDS != 'NO' ]]; then \
        update-alternatives --install /usr/bin/python3.12 python3.12 /usr/bin/python3.12 1 || true; \
    fi

# Copy source code to docker.
COPY . /srs
WORKDIR /srs/trunk

# Build and install SRS.
# Note that SRT is enabled by default, so we configure without --srt=on.
# Note that python_addons is enabled by default, requiring python3.12 and python3.12-venv.
# Note that we have copied all files by make install.
RUN ./configure ${CONFARGS} && make ${MAKEARGS} && make install

############################################################
# dist
############################################################
FROM ubuntu:24.04 AS dist

# https://serverfault.com/questions/949991/how-to-install-tzdata-on-a-ubuntu-docker-image
ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies including python3.12 and python3.12-venv for python_addons
# Also install ffmpeg for ingest and transcode features
RUN apt-get update && apt-get install -y \
    python3.12 python3.12-venv \
    ca-certificates \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create symlink for python3.12 to be available as python3.12 command
RUN update-alternatives --install /usr/bin/python3.12 python3.12 /usr/bin/python3.12 1 || true

# Expose ports for streaming @see https://github.com/ossrs/srs#ports
EXPOSE 1935 1985 8080 5060 9000 8000/udp 10080/udp

# SRS binary, config files and srs-console.
COPY --from=build /usr/local/srs /usr/local/srs

# Use system ffmpeg binary
# SRS expects ffmpeg at ./objs/ffmpeg/bin/ffmpeg
RUN mkdir -p /usr/local/srs/objs/ffmpeg/bin && \
    ln -sf /usr/bin/ffmpeg /usr/local/srs/objs/ffmpeg/bin/ffmpeg

# Test the version of binaries.
RUN /usr/local/srs/objs/ffmpeg/bin/ffmpeg -version && \
    ldd /usr/local/srs/objs/srs

VOLUME /usr/local/srs/conf
RUN mkdir -p /usr/local/srs/DVR_records
VOLUME /usr/local/srs/DVR_records

# Default workdir and command.
WORKDIR /usr/local/srs
ENV SRS_DAEMON=off SRS_IN_DOCKER=on
CMD ["./objs/srs", "-c", "conf/docker.conf"]