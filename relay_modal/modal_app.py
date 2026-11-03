"""Deploy the Muse Privacy relay on Modal.

    modal secret create muse-privacy-relay PAIRING_PEPPER=$(openssl rand -hex 32)   # once
    modal deploy relay_modal/modal_app.py

One container, always on, owns the SQLite file on a Volume and every device's WebSocket, so a
request for a device always finds its socket. The Volume is committed after each write. The
address is https://<workspace>--muse-privacy-relay.modal.run (RELAY_ORIGIN pins it, so OAuth
issuer and resource URLs never depend on a request's Host header).
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

APP_NAME = "muse-privacy-relay"
DATA = Path("/data")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi>=0.115", "httpx>=0.28")
    .add_local_python_source("relay_modal")
)
volume = modal.Volume.from_name("muse-privacy-relay-data", create_if_missing=True)
app = modal.App(APP_NAME)


@app.function(
    image=image,
    volumes={str(DATA): volume},
    secrets=[modal.Secret.from_name("muse-privacy-relay")],
    min_containers=1,
    max_containers=1,
    timeout=24 * 3600,
    cpu=0.25,
    memory=256,
)
@modal.concurrent(max_inputs=500)
@modal.asgi_app(label=APP_NAME)
def relay():
    from relay_modal.app import Settings, create_app
    from relay_modal.store import Store

    DATA.mkdir(parents=True, exist_ok=True)
    store = Store(DATA / "relay.db", persist=volume.commit)
    settings = Settings(pepper=os.environ.get("PAIRING_PEPPER"), origin=os.environ.get("RELAY_ORIGIN") or None)
    return create_app(store, settings)
