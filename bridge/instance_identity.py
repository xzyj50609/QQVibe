"""Stable local bridge identity for one resolved client installation."""

import hashlib
from pathlib import Path

from product_profile import load_product


def instance_id(root, product=None):
    # The product is part of the identity: one checkout can host both clients, and
    # neither may attach to the other's already-running bridge.
    # Resolve links and ignore case so every entry point names the same Windows root.
    canonical = f"{str(Path(root).resolve()).casefold()}|{load_product(product).instance_salt}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def default_port(root, product=None):
    # A collision is possible in this finite range; the launcher checks identity
    # before reuse and refuses to attach to any different occupant.
    return 20000 + int(instance_id(root, product)[:8], 16) % 40000
