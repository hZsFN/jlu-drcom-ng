"""Dr.COM (JLU) authentication client — Python reimplementation.

Modules
-------
``protocol``        packet construction and checksums (pure, fully unit-tested)
``md4``             pure-Python MD4, needed for keepalive CRC type 2
``binding``         UDP socket creation, bind-error classification, self-heal
``engine``          the auth state machine and keepalive loop
``controller``      owns every component; the seam the GUI and CLI share
``config``          JSON settings with an encrypted password field
``secrets_store``   DPAPI / Fernet / obfuscation password-at-rest protection
``logbus``          file + in-memory logging with account masking
``stats``           uptime and disconnect statistics
``traffic``         interface byte counters
``netprobe``        latency / loss probing
``notify``          desktop toast, webhook, external command
``statusapi``       local HTTP status API and status file
``ui``              the Flet HUD front-end
``cli``             headless mode
"""

from __future__ import annotations

__version__ = "1.1.0"
__all__ = ["__version__"]
