FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
COPY --chmod=0755 build-input/snell-server /usr/local/bin/snell-server
ENTRYPOINT ["/usr/local/bin/snell-server"]
CMD ["-c", "/run/secrets/snell.conf"]
