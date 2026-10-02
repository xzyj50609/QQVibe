"""Pure API portrait contract: revision and cache scope.

This module must stay dependency-free (standard library only) and must not import
services, adapters, storage, the runtime, or the message contract module.
"""
from __future__ import annotations

API_PORTRAIT_REVISION = "portrait-v2"


def api_portrait_scope(source_id):
    return source_id + ":" + API_PORTRAIT_REVISION
