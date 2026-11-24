# Odoo master (next, unreleased version) for the early-warning integration run.
# Upgrades the stable image in place with the latest nightly .deb from nightly.odoo.com.
# The .deb is checked against the SHA-256 in the matching .changes file.
FROM odoo:20.0@sha256:c7352c766b245b3c0e5c7b7a57e7660dde3265872be9566870d44bdb974f44cf

USER root
ARG NIGHTLY_URL=https://nightly.odoo.com/master/nightly/deb
RUN set -eu; \
    cd /tmp; \
    changes=$(curl -fsSL "$NIGHTLY_URL/" | grep -oE 'odoo_[0-9a-z.]+\.latest_amd64\.changes' | sort -uV | tail -1); \
    curl -fsSLo odoo.changes "$NIGHTLY_URL/$changes"; \
    set -- $(awk '/^Checksums-Sha256:/ {s=1; next} /^[^ ]/ {s=0} s && $3 ~ /_all\.deb$/ {print $1, $3}' odoo.changes); \
    sha="$1"; deb="$2"; \
    curl -fsSLo odoo.deb "$NIGHTLY_URL/$deb"; \
    echo "$sha  odoo.deb" | sha256sum -c -; \
    apt-get update; \
    apt-get -y install --no-install-recommends ./odoo.deb; \
    rm -rf /var/lib/apt/lists/* odoo.deb odoo.changes; \
    odoo --version
USER odoo
