"""Finite protocol bounds for 500,000 combined output characters.

JSON control-character escaping takes up to six bytes per character before
encryption and base64 expansion. The envelope allowance includes padding and
result metadata; HTTP framing has a separate small JSON metadata allowance.
"""

MAX_OUTPUT_CHARS = 500_000
MAX_ENVELOPE_CHARS = 4_194_304
MAX_REQUEST_BYTES = MAX_ENVELOPE_CHARS + 4096
