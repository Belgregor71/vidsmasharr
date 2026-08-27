# jellyfin-ffmpeg rather than Debian's ffmpeg: it ships VAAPI, QSV, the Intel
# media driver and libvmaf already built and wired together. Rolling our own
# ffmpeg for Apollo Lake is avoidable work, and Debian's build has no libvmaf.
FROM debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    \
    # Jellyfin's apt repo, for an ffmpeg that actually has VAAPI/QSV + libvmaf.
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/jellyfin.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/jellyfin.gpg] \
https://repo.jellyfin.org/debian bookworm main" \
        > /etc/apt/sources.list.d/jellyfin.list; \
    \
    # The better Intel drivers live in contrib/non-free, which the slim image
    # does not enable. Bookworm uses the deb822 format; older layouts use the
    # one-line format. Handle both, and do not fail if neither is present.
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -s /etc/apt/sources.list ]; then \
        sed -i 's/ main$/ main contrib non-free non-free-firmware/' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    \
    apt-get install -y --no-install-recommends \
        jellyfin-ffmpeg7 \
        python3 python3-pip python3-venv \
        # vainfo makes the capability report far more useful when VAAPI misbehaves
        vainfo \
        libva-drm2 libva2 \
        # Apollo Lake is Gen9. The free iHD driver handles it; i965 is the
        # fallback. Both are in main, so this is the reliable baseline.
        intel-media-va-driver \
        i965-va-driver; \
    \
    # The non-free builds add a few codecs. Nice to have, never worth failing
    # the build over -- jellyfin-ffmpeg also ships its own drivers (see
    # LIBVA_DRIVERS_PATH below), so this is belt and braces.
    apt-get install -y --no-install-recommends \
        intel-media-va-driver-non-free i965-va-driver-shaders \
        || echo "NOTE: non-free Intel drivers unavailable; using the free drivers."; \
    \
    apt-get purge -y curl gnupg; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/lib/jellyfin-ffmpeg:${PATH}" \
    VIDSMASHARR_FFMPEG=/usr/lib/jellyfin-ffmpeg/ffmpeg \
    VIDSMASHARR_FFPROBE=/usr/lib/jellyfin-ffmpeg/ffprobe \
    VIDSMASHARR_CONFIG_DIR=/config \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Colon-separated, tried in order. jellyfin-ffmpeg ships its own VAAPI drivers,
# so this still works if the distro driver packages are unavailable; the system
# path is the fallback.
ENV LIBVA_DRIVERS_PATH="/usr/lib/jellyfin-ffmpeg/lib/dri:/usr/lib/x86_64-linux-gnu/dri"

WORKDIR /app

COPY pyproject.toml /app/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir \
        "fastapi>=0.110" "uvicorn[standard]>=0.27" "jinja2>=3.1" \
        "pydantic>=2.6" "pydantic-settings>=2.2" "pyyaml>=6.0" \
        "httpx>=0.27" "apscheduler>=3.10" "python-multipart>=0.0.9"

ENV PATH="/opt/venv/bin:${PATH}"

COPY app /app/app
COPY bench /app/bench

VOLUME ["/config", "/scratch"]
EXPOSE 8330

# Default to the capability report: running the image with no arguments should
# tell you whether this NAS can do hardware encoding at all, which is the first
# question worth answering.
ENTRYPOINT ["python3", "-m"]
CMD ["bench.capability"]
