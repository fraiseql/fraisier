#!/bin/sh
set -eu
: "${AUTHORIZED_KEY:?the sshd fixture passes the session key}"
for u in nopw pw nocache zshu; do
    printf '%s\n' "$AUTHORIZED_KEY" > "/home/$u/.ssh/authorized_keys"
    chown "$u" "/home/$u/.ssh/authorized_keys"
    chmod 600 "/home/$u/.ssh/authorized_keys"
done
exec /usr/sbin/sshd -D -e
