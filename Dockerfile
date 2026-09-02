# Isolated Game Boy ROM validator image.
# Built for --network=none / --read-only / --cap-drop=ALL runs from the MCP tool.
FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin validator \
    && mkdir -p /work /opt/validator \
    && chown validator:validator /work

COPY docker/validate_gb_rom.py /opt/validator/validate_gb_rom.py
RUN chmod 755 /opt/validator/validate_gb_rom.py

USER validator
WORKDIR /work

# No ENTRYPOINT that auto-runs; the MCP host starts the container empty,
# copies the candidate ROM in, then execs the validator.
CMD ["sleep", "infinity"]
