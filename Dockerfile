FROM gcr.io/distroless/cc-debian13@sha256:de02e95a878d4662f7a0f93bffb3799e08e04cc5a53f6a9c0593a20eaf4dc80e AS cxx
FROM gcr.io/distroless/base-nossl-debian13@sha256:3ef37b0ae7c2b13bdf0ab02119080360cb424ff6c153a6f4c85b308bb1515f89
USER 0:0
# Copy the C++ runtime and its package records without bringing OpenSSL into the image.
COPY --from=cxx /usr/lib/x86_64-linux-gnu/libgcc_s.so.1 /usr/lib/x86_64-linux-gnu/libstdc++.so.6* /usr/lib/x86_64-linux-gnu/
COPY --from=cxx /var/lib/dpkg/status.d/gcc-14-base* /var/lib/dpkg/status.d/libgcc-s1* /var/lib/dpkg/status.d/libstdc++6* /var/lib/dpkg/status.d/
COPY --chmod=0555 build-input/snell-server /usr/local/bin/snell-server
ENTRYPOINT ["/usr/local/bin/snell-server"]
CMD ["-c", "/run/secrets/snell.conf"]
