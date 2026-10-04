# Build stage for frontend
FROM node:20-alpine AS frontend-builder

WORKDIR /app/web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# Python application stage
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies. The WireGuard endpoint (docs/wireguard.md)
# needs iproute2, wireguard-tools and nftables, plus wireguard-go for hosts
# whose kernel has no WireGuard. The process runs unprivileged, so the tools
# are launched through setpriv, which carries CAP_NET_ADMIN as a file
# capability and hands it on as an ambient capability. (A file capability
# on ip itself is not enough: iproute2 drops its capabilities when run by a
# non-root user unless they are inheritable.) The container still has to be
# granted the capability (cap_add: [NET_ADMIN]).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    iproute2 \
    libcap2-bin \
    nftables \
    wireguard-tools \
    wireguard-go \
    && rm -rf /var/lib/apt/lists/* \
    && setcap cap_net_admin,cap_net_raw+ep "$(readlink -f "$(command -v setpriv)")"

# Copy Python dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir -e .

# Copy application code
COPY api/ ./api/
COPY config/ ./config/

# Copy built frontend
COPY --from=frontend-builder /app/web/dist ./web/dist

# Create non-root user and data directories. /run/wireguard is where
# wireguard-go (the userspace fallback) puts the control socket wg talks to;
# it must be writable by the unprivileged user.
RUN useradd -m -u 1000 octoprox \
    && mkdir -p /app/data/ca /app/data/geo /run/wireguard \
    && chown -R octoprox:octoprox /app /run/wireguard
USER octoprox

# Expose ports (51820/udp is the WireGuard endpoint, used when wireguard.enabled)
EXPOSE 8000 8080 51820/udp

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]

