"""hermes-cloud-scratch — personal test scaffolding, do not use.

The interesting code is the dashboard backend in ``dashboard/plugin_api.py``.
This agent-side hook only proves the plugin loaded into agent processes too.
"""

import logging

_log = logging.getLogger("hermes-cloud-scratch")


def register(ctx):
    _log.info("hermes-cloud-scratch registered (agent side)")
