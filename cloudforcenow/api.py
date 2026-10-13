"""Async client for the Vast.ai REST API (console.vast.ai/api/v0)."""

from __future__ import annotations

import json
from typing import Any

import httpx

BASE_URL = "https://console.vast.ai/api"

# RTX 2000 / 3000 / 4000 / 5000 series as named in the Vast.ai marketplace.
# Unknown names in an "in" filter are harmless, so this list errs on the wide side.
GPU_NAMES = [
    "RTX 2060", "RTX 2060S", "RTX 2060 Super", "RTX 2070", "RTX 2070S",
    "RTX 2070 Super", "RTX 2080", "RTX 2080S", "RTX 2080 Super", "RTX 2080 Ti",
    "RTX 3050", "RTX 3060", "RTX 3060 Ti", "RTX 3070", "RTX 3070 Ti",
    "RTX 3080", "RTX 3080 Ti", "RTX 3090", "RTX 3090 Ti",
    "RTX 4060", "RTX 4060 Ti", "RTX 4070", "RTX 4070S", "RTX 4070 Super",
    "RTX 4070 Ti", "RTX 4070S Ti", "RTX 4070 Ti Super",
    "RTX 4080", "RTX 4080S", "RTX 4080 Super", "RTX 4090", "RTX 4090D",
    "RTX 5060", "RTX 5060 Ti", "RTX 5070", "RTX 5070 Ti",
    "RTX 5080", "RTX 5090",
]

# All selectable series (order = display order in the filters modal).
# The default *selection* lives in Config.gpu_series; 2000 is available but off.
GPU_SERIES = ["2000", "3000", "4000", "5000"]


def gpu_names_for_series(series: list[str]) -> list[str]:
    """GPU marketplace names belonging to any of the given series (e.g. "4000")."""
    wanted = set(series)
    return [name for name in GPU_NAMES if _series_of(name) in wanted]


def _series_of(name: str) -> str:
    """"RTX 4070 Ti" -> "4000"; "" if no leading model digit found."""
    for part in name.split():
        if part and part[0].isdigit():
            return part[0] + "000"
    return ""

# Same regions, labels and order as the cloud.vast.ai region selector.
REGIONS: dict[str, list[str]] = {
    "Planet Earth": [],
    "North America": ["US", "CA", "MX"],
    "South America": ["AR", "BO", "BR", "CL", "CO", "EC", "PE", "PY", "UY", "VE"],
    "Europe": [
        "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LT", "LU", "LV",
        "MT", "NL", "NO", "PL", "PT", "RO", "RS", "SE", "SI", "SK", "UA",
    ],
    "Asia": [
        "AE", "CN", "HK", "ID", "IL", "IN", "JP", "KR", "KZ", "MY", "PH",
        "PK", "SA", "SG", "TH", "TR", "TW", "VN",
    ],
    "Africa": ["EG", "KE", "MA", "NG", "ZA"],
    "Oceania": ["AU", "NZ"],
}


class VastError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class VastClient:
    def __init__(self, api_key: str):
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise VastError(f"network error: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = data.get("msg") or data.get("error") or ""
            # Vast is migrating endpoints from v0 to v1 one at a time;
            # retry on v1 when v0 reports itself deprecated.
            if "deprecated" in msg and path.startswith("/v0/"):
                return await self._request(method, "/v1" + path[3:], **kwargs)
            raise VastError(
                msg or f"HTTP {resp.status_code} on {method} {path}",
                status=resp.status_code,
            )
        return data

    # -- account ------------------------------------------------------------

    async def current_user(self) -> dict:
        return await self._request("GET", "/v0/users/current/")

    # -- offers -------------------------------------------------------------

    async def search_offers(
        self,
        region_codes: list[str],
        max_dph: float | None,
        *,
        gpu_series: list[str] = GPU_SERIES,
        min_cpu_ghz: float = 4.0,
        min_reliability: float = 92.0,  # percent
        min_disk: int = 120,
        max_net_cost: float = 5.0,  # $/TB
    ) -> list[dict]:
        """Search rentable single-GPU, VM-capable offers matching the filters.

        reliability is a percent (92 -> 0.92) and net cost is $/TB; the API
        wants a 0-1 fraction and $/GB, so both are converted here.
        """
        query: dict[str, Any] = {
            "limit": 150,
            "type": "ondemand",
            "rentable": {"eq": True},
            "vms_enabled": {"eq": True},
            "num_gpus": {"eq": 1},
            "gpu_name": {"in": gpu_names_for_series(gpu_series)},
            "cpu_ghz": {"gte": min_cpu_ghz},
            "reliability": {"gte": min_reliability / 100.0},
            "disk_space": {"gte": min_disk},
            "inet_up_cost": {"lte": max_net_cost / 1000.0},
            "inet_down_cost": {"lte": max_net_cost / 1000.0},
            "order": [["dph_total", "asc"]],
        }
        if region_codes:
            query["geolocation"] = {"in": region_codes}
        if max_dph is not None:
            query["dph_total"] = {"lte": max_dph}
        data = await self._request("POST", "/v0/bundles/", json=query)
        return data.get("offers", []) if isinstance(data, dict) else []

    # -- templates ----------------------------------------------------------

    async def find_template_hash(self, name: str) -> str:
        params = {"select_filters": json.dumps({"name": {"eq": name}})}
        data = await self._request("GET", "/v0/template/", params=params)
        templates = data.get("templates", []) if isinstance(data, dict) else []
        for tpl in templates:
            if tpl.get("hash_id"):
                return tpl["hash_id"]
        raise VastError(f"template {name!r} not found")

    # -- instances ----------------------------------------------------------

    async def create_instance(
        self, ask_id: int, template_hash: str, disk: int, label: str
    ) -> dict:
        body = {
            "template_hash_id": template_hash,
            "vm": True,
            "disk": disk,
            "label": label,
            "target_state": "running",
        }
        return await self._request("PUT", f"/v0/asks/{ask_id}/", json=body)

    async def instances(self) -> list[dict]:
        data = await self._request("GET", "/v1/instances/")
        return data.get("instances", []) if isinstance(data, dict) else []

    async def start_instance(self, instance_id: int) -> None:
        await self._request("PUT", f"/v0/instances/{instance_id}/", json={"state": "running"})

    async def stop_instance(self, instance_id: int) -> None:
        await self._request("PUT", f"/v0/instances/{instance_id}/", json={"state": "stopped"})

    async def reboot_instance(self, instance_id: int) -> None:
        await self._request("PUT", f"/v0/instances/reboot/{instance_id}/")

    async def destroy_instance(self, instance_id: int) -> None:
        await self._request("DELETE", f"/v0/instances/{instance_id}/")
