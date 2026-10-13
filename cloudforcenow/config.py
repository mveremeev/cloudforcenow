"""Persistent config: API key and search preferences."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cloudforcenow"
)
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class Config:
    api_key: str = ""
    region: str = "Planet Earth"
    max_dph: float | None = None  # $/hr cap, None = no cap
    disk: int = 120  # GB requested at instance creation
    template_name: str = "Ubuntu Desktop (VM)"
    template_hash: str = ""  # resolved lazily from template_name, then cached

    # Advanced search constraints (editable under "advanced" in the filters modal).
    gpu_series: list[str] = field(default_factory=lambda: ["3000", "4000", "5000"])
    min_cpu_ghz: float = 4.0
    min_reliability: float = 92.0  # percent
    min_disk: int = 120  # GB the host must offer
    max_net_cost: float = 5.0  # $/TB, up and down

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n")
        CONFIG_FILE.chmod(0o600)
