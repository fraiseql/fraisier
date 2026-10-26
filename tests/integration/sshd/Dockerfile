# A real sshd for the SSH runner tests (see the `sshd` fixture in
# tests/integration/conftest.py). The key is not baked in: the entrypoint
# installs $AUTHORIZED_KEY, so the image stays cacheable across sessions.
#
# Users, all with password `s3cr3t`:
#   nopw     sudo NOPASSWD
#   pw       sudo with a password
#   nocache  sudo with a password and timestamp_timeout=0 (no ticket survives)
#   zshu     sudo with a password, zsh login shell
FROM debian:bookworm-slim
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends openssh-server sudo procps zsh >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && for u in nopw pw nocache zshu; do \
      useradd -m -s /bin/bash "$u" && echo "$u:s3cr3t" | chpasswd \
      && install -d -m 700 -o "$u" "/home/$u/.ssh"; \
    done \
 && chsh -s /usr/bin/zsh zshu && touch /home/zshu/.zshrc \
 && echo 'nopw ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/nopw \
 && echo 'pw ALL=(ALL) ALL' > /etc/sudoers.d/pw \
 && printf 'nocache ALL=(ALL) ALL\nDefaults:nocache timestamp_timeout=0\n' > /etc/sudoers.d/nocache \
 && echo 'zshu ALL=(ALL) ALL' > /etc/sudoers.d/zshu
COPY entrypoint.sh /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
