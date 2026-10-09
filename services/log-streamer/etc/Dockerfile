FROM tecnativa/docker-socket-proxy:0.4.2@sha256:1f3a6f303320723d199d2316a3e82b2e2685d86c275d5e3deeaf182573b47476

# v1.15 (FDY-0558): Override the default HAProxy configuration so that
# CONTAINERS=1 does NOT allow archive/env/top/exec routes.  The v0.4.2
# image does not support ARCHIVE/ENV/TOP/CREATES environment variables,
# so we replace the generated config with a custom one that explicitly
# denies those routes before the blanket CONTAINERS:1 allow rule.

COPY docker-socket-proxy.cfg /etc/haproxy/haproxy.cfg
