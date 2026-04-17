"""
MCP server bridging Copilot CLI to MT5 Bridge.
MCP server สำหรับเชื่อม Copilot CLI เข้ากับ MT5 Bridge.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Literal

import httpx
from fastmcp import FastMCP

BASE_URL = os.getenv("MT5_BRIDGE_BASE_URL", "http://localhost:8000")

mcp = FastMCP(
    "mt5-bridge",
    instructions=(
        "Use this tool to retrieve MT5 chart data, inspect positions, and place live trading orders. "
        "Always use it when rate retrieval or chart analysis is requested."
    ),
)

def _request(
    method: str,
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    # Single HTTP request wrapper with consistent error handling /
    # ตัวห่อสำหรับยิง HTTP request หนึ่งครั้งพร้อมจัดการ error ให้สม่ำเสมอ
    try:
        with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
            response = client.request(method, path, json=json, params=params)
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.text
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise RuntimeError(f"MT5 Bridge error {exc.response.status_code}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Failed to reach MT5 Bridge: {exc}") from exc


@mcp.tool()
def health() -> Dict[str, Any]:
    """
    Check the health status of the MT5 Bridge.
    ตรวจสอบสถานะสุขภาพของ MT5 Bridge.
    """
    return _request("GET", "/health")


@mcp.tool()
def get_rates(symbol: str, timeframe: str = "M1", count: int = 100) -> List[Dict[str, Any]]:
    """
    Fetch OHLCV bars.
    ดึงข้อมูลแท่งราคา OHLCV.
    """
    return _request("GET", f"/rates/{symbol}", params={"timeframe": timeframe, "count": count})


@mcp.tool()
def get_tick(symbol: str) -> Dict[str, Any]:
    """
    Fetch the latest tick.
    ดึง tick ล่าสุด.
    """
    return _request("GET", f"/tick/{symbol}")


@mcp.tool()
def list_positions() -> List[Dict[str, Any]]:
    """
    List open positions.
    แสดงรายการ position ที่ยังเปิดอยู่.
    """
    return _request("GET", "/positions")


@mcp.tool()
def get_history_deals(
    start: Optional[str] = None,
    end: Optional[str] = None,
    group: Optional[str] = None,
    ticket: Optional[int] = None,
    position: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch MT5 history deals by range, order ticket, or position ticket.
    ดึง MT5 history deals ตามช่วงเวลา, order ticket หรือ position ticket.
    """
    params: Dict[str, Any] = {}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    if group:
        params["group"] = group
    if ticket is not None:
        params["ticket"] = ticket
    if position is not None:
        params["position"] = position
    return _request("GET", "/history/deals", params=params)


@mcp.tool()
def send_order(
    symbol: str,
    side: Literal["BUY", "SELL"],
    volume: float,
    sl: float = 0.0,
    tp: float = 0.0,
    comment: str = "",
) -> Dict[str, Any]:
    """
    Submit a market order.
    ส่ง market order ไปยัง MT5 Bridge.
    """
    payload = {"symbol": symbol, "type": side, "volume": volume, "sl": sl, "tp": tp, "comment": comment}
    return _request("POST", "/order", json=payload)


@mcp.tool()
def close_position(ticket: int) -> Dict[str, Any]:
    """
    Close a position by ticket.
    ปิด position ตาม ticket ที่ระบุ.
    """
    return _request("POST", "/close", json={"ticket": ticket})


@mcp.tool()
def modify_position(
    ticket: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    update_sl: bool = False,
    update_tp: bool = False,
) -> Dict[str, Any]:
    """
    Update SL/TP for a position.
    ปรับค่า SL/TP ของ position ที่ระบุ.
    """
    payload = {"ticket": ticket, "sl": sl, "tp": tp, "update_sl": update_sl, "update_tp": update_tp}
    return _request("POST", "/modify", json=payload)



if __name__ == "__main__":
    import argparse

    # Parse CLI arguments so the operator can configure API URL and listen address /
    # แปลง argument จาก CLI เพื่อให้ผู้ใช้งานตั้งค่า API base URL และที่อยู่สำหรับรับการเชื่อมต่อได้
    parser = argparse.ArgumentParser(description="Run MT5 Bridge MCP over HTTP")
    parser.add_argument("--http", action="store_true", help="Run MCP over HTTP (default: stdio)")
    parser.add_argument("--api-base", default=BASE_URL, help="MT5 Bridge API base URL (default: env MT5_BRIDGE_BASE_URL or http://localhost:8000)")
    parser.add_argument("--host", default="0.0.0.0", help="MCP listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001, help="MCP listen port (default: 8001)")
    args = parser.parse_args()

    # Override BASE_URL using the CLI value /
    # เขียนทับ BASE_URL จากค่าที่ส่งมาทาง CLI
    BASE_URL = args.api_base

    if args.http:
        mcp.run(host=args.host, port=args.port, transport="http")
    else:
        mcp.run(transport="stdio")
