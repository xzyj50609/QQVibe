"""Product identity shared by the Python bridge and the Electron shell.

One manifest names both products so the QQ client can never inherit the WeChat data
root, bridge instance/port, control token or upstream update feed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "scripts" / "product-identity.json"
PRODUCT_ENV = "QQVIBE_PRODUCT"
ACCOUNTS_DIRECTORY = "accounts"

REQUIRED_FIELDS = ("productName", "appId", "dataDir", "instanceSalt", "controlTokenEnv",
                   "controlTokenHeader", "storageNamespace", "updateRepository", "iconPng", "iconIco")
ACCOUNT_KEY_PATTERN = re.compile(r"a:[0-9a-f]{32}")

_CACHE: dict[str, "ProductProfile"] = {}


@dataclass(frozen=True)
class ProductProfile:
    key: str
    product_name: str
    app_id: str
    data_dir: str
    instance_salt: str
    control_token_env: str
    control_token_header: str
    storage_namespace: str
    update_repository: str | None
    icon_png: str
    icon_ico: str
    display_name: str = ""

    @property
    def update_channel_enabled(self) -> bool:
        return bool(self.update_repository)

    def data_root(self, root=ROOT) -> Path:
        return Path(root) / self.data_dir

    def state_dir(self, name, root=ROOT) -> Path:
        return self.data_root(root) / name

    def account_directory(self, account_key, root=ROOT) -> Path:
        """Windows cannot use the colon-bearing key as a directory name, so only the hex."""
        if ACCOUNT_KEY_PATTERN.fullmatch(account_key or "") is None:
            raise ValueError("invalid account key")
        return self.data_root(root) / ACCOUNTS_DIRECTORY / account_key[2:]


def _manifest() -> dict:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    products = document.get("products") if isinstance(document, dict) else None
    if document.get("schema") != 1 or not isinstance(products, dict) or not products:
        raise RuntimeError("product identity manifest is unreadable")
    return document


def load_product(key: str | None = None) -> ProductProfile:
    document = _manifest()
    chosen = key or os.environ.get(PRODUCT_ENV) or document["default"]
    cached = _CACHE.get(chosen)
    if cached is not None:
        return cached
    record = document["products"].get(chosen)
    if not isinstance(record, dict):
        raise ValueError(f"unknown product: {chosen}")
    missing = [name for name in REQUIRED_FIELDS if name not in record]
    if missing:
        raise RuntimeError(f"product identity {chosen} is missing {missing}")
    for field, extension in (("iconPng", "png"), ("iconIco", "ico")):
        value = record[field]
        if not isinstance(value, str) or not re.fullmatch(r"chatui/assets/[a-z0-9-]+\." + extension, value):
            raise ValueError("invalid product icon path")
    profile = ProductProfile(key=chosen, product_name=record["productName"],
                             app_id=record["appId"], data_dir=record["dataDir"],
                             instance_salt=record["instanceSalt"],
                             control_token_env=record["controlTokenEnv"],
                             control_token_header=record["controlTokenHeader"],
                             storage_namespace=record["storageNamespace"],
                             update_repository=record["updateRepository"],
                             icon_png=record["iconPng"], icon_ico=record["iconIco"],
                             display_name=record.get("displayName", record["productName"]))
    _CACHE[chosen] = profile
    return profile


def current_product() -> ProductProfile:
    return load_product()


def user_data_directory_names() -> list[str]:
    """Every product's local data directory, so no update package can carry one as files."""
    document = _manifest()
    return sorted({str(record["dataDir"]).casefold() for record in document["products"].values()
                   if isinstance(record, dict) and record.get("dataDir")})
