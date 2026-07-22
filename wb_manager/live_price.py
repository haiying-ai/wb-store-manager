from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import WbClient
from .endpoints import ApiDomain


def set_prices(
    client: WbClient,
    items: list[dict[str, int]],
    *,
    operation_name: str,
    output_dir: Path,
    readback_delay: float = 5.0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    request = {"data": items}
    response = client.request("POST", f"{ApiDomain.PRICES}/api/v2/upload/task", body=request)
    if readback_delay > 0:
        time.sleep(readback_delay)
    readback = read_prices(client, [item["nmID"] for item in items])
    report = {
        "timestamp": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": operation_name,
        "request": request,
        "response": {"status": response.status, "ok": response.ok, "data": response.data},
        "readback": readback,
    }
    path = output_dir / f"{operation_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["path"] = str(path)
    return report


def read_prices(client: WbClient, nm_ids: list[int]) -> dict[str, Any]:
    result = client.request("GET", f"{ApiDomain.PRICES}/api/v2/list/goods/filter?limit=1000&offset=0")
    goods = _get(result.data, ["data", "listGoods"], [])
    wanted = set(nm_ids)
    if isinstance(goods, list):
        goods = [item for item in goods if item.get("nmID") in wanted]
    else:
        goods = []
    return {"status": result.status, "ok": result.ok, "price": goods}


def read_price_task(client: WbClient, upload_id: int) -> dict[str, Any]:
    result = client.request(
        "GET",
        f"{ApiDomain.PRICES}/api/v2/history/goods/task?limit=1000&offset=0&uploadID={upload_id}",
    )
    return {"status": result.status, "ok": result.ok, "data": result.data}


def read_price_quarantine(client: WbClient, nm_ids: list[int]) -> dict[str, Any]:
    result = client.request("GET", f"{ApiDomain.PRICES}/api/v2/quarantine/goods?limit=1000&offset=0")
    goods = _get(result.data, ["data", "quarantineGoods"], [])
    wanted = set(nm_ids)
    if isinstance(goods, list):
        goods = [item for item in goods if item.get("nmID") in wanted]
    else:
        goods = []
    return {"status": result.status, "ok": result.ok, "quarantine": goods}


def _get(source: Any, path: list[str], default: Any) -> Any:
    current = source
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default
