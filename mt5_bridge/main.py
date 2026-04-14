#!/usr/bin/env python3

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
import asyncio
import argparse
import os
import sys
import json
from datetime import datetime, timezone
import pandas as pd
from importlib.metadata import version, PackageNotFoundError
from contextlib import asynccontextmanager

# Try relative imports (package mode), fallback to path manipulation (script mode)
try:
    from .mt5_handler import MT5Handler
    from .client import BridgeClient
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mt5_bridge.mt5_handler import MT5Handler
    from mt5_bridge.client import BridgeClient

mt5_handler = MT5Handler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the startup and shutdown lifecycle for the MT5 connection."""
    # Initialize MT5 only on Windows and keep background reconnection checks running.
    if sys.platform == "win32":
        if not mt5_handler.initialize():
            print("WARNING: Failed to initialize MT5 on startup. Will retry in background.")

        monitor_task = asyncio.create_task(monitor_connection())
    else:
        print("Non-Windows platform detected: MT5 connection disabled.")
        monitor_task = None

    try:
        yield
    finally:
        # Stop the background monitor first, then close the MT5 session cleanly.
        if monitor_task:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        mt5_handler.shutdown()

app = FastAPI(title="MT5 Bridge API", lifespan=lifespan)

def parse_datetime(val: str) -> int:
    """Parse a string as a unix timestamp or a datetime string."""
    try:
        # Try as a numeric timestamp first
        return int(float(val))
    except ValueError:
        # Try as a datetime string using pandas for flexibility
        dt = pd.to_datetime(val)
        if dt.tzinfo is None:
            # Assume UTC if no timezone is provided
            dt = dt.tz_localize('UTC')
        else:
            # Convert to UTC if a timezone is provided
            dt = dt.tz_convert('UTC')
        return int(dt.timestamp())

class Rate(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int

class Tick(BaseModel):
    time: int
    time_msc: int
    bid: float
    ask: float
    last: float
    volume: int

class HistoricalTick(BaseModel):
    """Historical tick data model with millisecond precision."""
    time: int              # Timestamp in seconds (UTC)
    time_msc: int          # Timestamp in milliseconds
    bid: float
    ask: float
    last: float
    volume: int
    flags: int             # Tick change flags (Bid/Ask/Last/Volume updates)

class BookItem(BaseModel):
    """Market depth item model."""
    type: str              # BUY, SELL, BUY_LIMIT, SELL_LIMIT, OTHER
    price: float
    volume: float
    #volume_real: float
    volume_dbl: float

class Account(BaseModel):
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    leverage: int
    currency: str
    server: str

class Position(BaseModel):
    ticket: int
    symbol: str
    type: str
    volume: float
    price_open: float
    comment: str
    magic: int
    sl: float
    tp: float
    price_current: float
    profit: float
    time: int
    time_msc: int

class HistoryDeal(BaseModel):
    ticket: int
    order: int
    time: int
    time_msc: int
    type: str
    type_code: int
    entry: str
    entry_code: int
    magic: int
    position_id: int
    reason: str
    reason_code: int
    volume: float
    price: float
    commission: float
    swap: float
    profit: float
    fee: float
    symbol: str
    comment: str
    external_id: str

async def monitor_connection():
    """Periodically check MT5 connection and reconnect if needed."""
    while True:
        try:
            if not mt5_handler.check_connection():
                print("WARNING: MT5 connection lost. Reconnecting...")
            await asyncio.sleep(5)  # Check every 5 seconds
        except Exception as e:
            print(f"Error in connection monitor: {e}")
            await asyncio.sleep(5)

@app.get("/health")
def health_check():
    return {"status": "ok", "mt5_connected": mt5_handler.connected}

@app.get("/rates/{symbol}", response_model=List[Rate])
def get_rates(
    symbol: str, 
    timeframe: str = Query(..., description="Timeframe (e.g., M1, H1)"), 
    count: int = Query(1000, description="Number of bars")
):
    rates = mt5_handler.get_rates(symbol, timeframe, count)
    if rates is None:
        raise HTTPException(status_code=500, detail=f"Failed to get rates for {symbol}")
    return rates

@app.get("/rates_range/{symbol}", response_model=List[Rate])
def get_rates_range(
    symbol: str,
    timeframe: str = Query(..., description="Timeframe (e.g., M1, H1)"),
    start: str = Query(..., description="Start timestamp or datetime string (UTC)"),
    end: str = Query(..., description="End timestamp or datetime string (UTC)")
):
    try:
        start_ts = parse_datetime(start)
        end_ts = parse_datetime(end)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

    date_from = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    date_to = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    rates = mt5_handler.get_rates_range(symbol, timeframe, date_from, date_to)
    if rates is None:
        raise HTTPException(status_code=500, detail=f"Failed to get rates range for {symbol}")
    return rates

@app.get("/tick/{symbol}", response_model=Tick)
def get_tick(symbol: str):
    tick = mt5_handler.get_tick(symbol)
    if tick is None:
        raise HTTPException(status_code=500, detail=f"Failed to get tick for {symbol}")
    return tick

@app.get("/book/{symbol}", response_model=List[BookItem])
def get_book(symbol: str):
    book = mt5_handler.get_market_book(symbol)
    if book is None:
        raise HTTPException(status_code=500, detail=f"Failed to get market book for {symbol}")
    return book

@app.get("/ticks_from/{symbol}", response_model=List[HistoricalTick])
def get_ticks_from(
    symbol: str,
    start: str = Query(..., description="Start timestamp or datetime string (UTC)"),
    count: int = Query(1000, description="Number of ticks to retrieve"),
    flags: str = Query("ALL", description="Tick type: ALL, INFO (bid/ask changes), TRADE (last/volume changes)")
):
    """
    Retrieve historical tick data starting from the specified datetime.

    This can be used for workloads such as reinforcement learning training
    data for short-term scalping strategies.
    """
    try:
        start_ts = parse_datetime(start)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    
    date_from = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    ticks = mt5_handler.get_ticks_from(symbol, date_from, count, flags)
    if ticks is None:
        raise HTTPException(status_code=500, detail=f"Failed to get ticks from {symbol}")
    return ticks

@app.get("/ticks_range/{symbol}", response_model=List[HistoricalTick])
def get_ticks_range(
    symbol: str,
    start: str = Query(..., description="Start timestamp or datetime string (UTC)"),
    end: str = Query(..., description="End timestamp or datetime string (UTC)"),
    flags: str = Query("ALL", description="Tick type: ALL, INFO (bid/ask changes), TRADE (last/volume changes)")
):
    """
    Retrieve historical tick data within the specified datetime range.

    This is useful for workflows such as reinforcement learning training
    data for short-term scalping strategies.
    Note: requests for large amounts of tick data may take some time.
    """
    try:
        start_ts = parse_datetime(start)
        end_ts = parse_datetime(end)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    
    date_from = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    date_to = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    ticks = mt5_handler.get_ticks_range(symbol, date_from, date_to, flags)
    if ticks is None:
        raise HTTPException(status_code=500, detail=f"Failed to get ticks range for {symbol}")
    return ticks

@app.get("/account", response_model=Account)
def get_account():
    account = mt5_handler.get_account_info()
    if account is None:
        raise HTTPException(status_code=500, detail="Failed to get account info")
    return account

@app.get("/positions", response_model=List[Position])
def get_positions(
    symbols: Optional[str] = Query(None, description="Comma-separated list of symbols to filter (e.g., 'XAUUSD,BTCUSD')"),
    magic: Optional[int] = Query(None, description="Magic number to filter positions by"),
):
    # Convert the symbols parameter to a list when provided.
    symbol_list = None
    if symbols:
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            symbol_list = None
    
    positions = mt5_handler.get_positions(symbols=symbol_list, magic=magic)
    if positions is None:
        raise HTTPException(status_code=500, detail="Failed to get positions")
    return positions

@app.get("/history/deals", response_model=List[HistoryDeal])
def get_history_deals(
    start: Optional[str] = Query(None, description="Start timestamp or datetime string (UTC)"),
    end: Optional[str] = Query(None, description="End timestamp or datetime string (UTC)"),
    group: Optional[str] = Query(None, description="Optional MT5 group filter, e.g. '*USD*' or '*, !EUR'"),
    ticket: Optional[int] = Query(None, description="Order ticket filter"),
    position: Optional[int] = Query(None, description="Position ticket filter"),
):
    """
    Retrieve MT5 trading history deals.
    Supports the same lookup styles as history_deals_get: range, ticket, or position.
    """
    # Reject ambiguous requests early / 曖昧な検索条件を早めに拒否する
    if ticket is not None and position is not None:
        raise HTTPException(status_code=400, detail="Specify either ticket or position, not both")

    date_from = None
    date_to = None

    # Range mode requires both start and end / 期間検索では start と end が両方必要
    if ticket is None and position is None:
        if start is None or end is None:
            raise HTTPException(status_code=400, detail="Provide start/end, or specify ticket, or specify position")
        try:
            start_ts = parse_datetime(start)
            end_ts = parse_datetime(end)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")

        date_from = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        date_to = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    deals = mt5_handler.get_history_deals(
        date_from=date_from,
        date_to=date_to,
        group=group,
        ticket=ticket,
        position=position,
    )
    if deals is None:
        raise HTTPException(status_code=500, detail="Failed to get history deals")
    return deals

class OrderRequest(BaseModel):
    symbol: str
    type: str # "BUY" or "SELL"
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    magic: int = 123456

class CloseRequest(BaseModel):
    ticket: int

class ModifyRequest(BaseModel):
    ticket: int
    sl: Optional[float] = None
    tp: Optional[float] = None
    update_sl: bool = False
    update_tp: bool = False

@app.post("/order")
def send_order(order: OrderRequest):
    ticket, error = mt5_handler.send_order(
        order.symbol, 
        order.type, 
        order.volume, 
        order.sl, 
        order.tp, 
        order.comment,
        magic=order.magic
    )
    if ticket is None:
        detail = error or "Failed to send order"
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "ticket": ticket}

@app.post("/close")
def close_position(req: CloseRequest):
    success, message = mt5_handler.close_position(req.ticket)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to close position: {message}")
    return {"status": "ok"}

@app.post("/modify")
def modify_position(req: ModifyRequest):
    success, message = mt5_handler.modify_position(
        req.ticket,
        req.sl,
        req.tp,
        req.update_sl,
        req.update_tp,
    )
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to modify position: {message}")
    return {"status": "ok"}

def main():
    parser = argparse.ArgumentParser(description="MT5 Bridge CLI")
    
    try:
        app_version = version("mt5-bridge")
    except PackageNotFoundError:
        app_version = "unknown"

    parser.add_argument(
        "--version",
        action="version",
        version=f"mt5-bridge version: {app_version}\nPython version: {sys.version}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Serve command
    server_parser = subparsers.add_parser("server", help="Run MT5 Bridge Server (Windows Only)")
    server_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host interface to bind (default: 0.0.0.0)",
    )
    server_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000)",
    )
    server_parser.add_argument("--mt5-path", default=None, help="Path to MT5 executable")
    server_parser.add_argument("--mt5-login", type=int, default=None, help="MT5 Login ID")
    server_parser.add_argument("--mt5-password", default=None, help="MT5 Password")
    server_parser.add_argument("--mt5-server", default=None, help="MT5 Server Name")
    server_parser.add_argument("--no-utc", action="store_true", help="Disable UTC conversion")

    # Client command
    client_parser = subparsers.add_parser("client", help="Run MT5 Bridge Client")
    client_parser.add_argument("--url", default="http://localhost:8000", help="Server URL")
    
    client_subs = client_parser.add_subparsers(dest="client_command", help="Client command", required=True)
    
    # Client Subcommands
    client_subs.add_parser("health", help="Check server health")
    
    rates_p = client_subs.add_parser("rates", help="Get historical rates")
    rates_p.add_argument("symbol", type=str)
    rates_p.add_argument("--timeframe", default="M1")
    rates_p.add_argument("--count", type=int, default=1000)
    
    # Command for retrieving historical rates by date range.
    rates_range_p = client_subs.add_parser("rates_range", help="Get historical rates by date range")
    rates_range_p.add_argument("symbol", type=str)
    rates_range_p.add_argument("--timeframe", default="M1", help="Timeframe (e.g. M1, H1)")
    rates_range_p.add_argument("--start", type=str, required=True, help="Start timestamp or datetime string (e.g. 2025-01-01)")
    rates_range_p.add_argument("--end", type=str, required=True, help="End timestamp or datetime string (e.g. 2025-01-01 12:00)")

    tick_p = client_subs.add_parser("tick", help="Get latest tick")
    tick_p.add_argument("symbol", type=str)
    
    client_subs.add_parser("account", help="Get account information")

    positions_p = client_subs.add_parser("positions", help="Get open positions")
    positions_p.add_argument("--symbols", help="Comma-separated list of symbols (e.g. BTCUSD,ETHUSD)")
    positions_p.add_argument("--magic", type=int, help="Magic number filter")

    # History deals command / 履歴 deal 取得コマンド
    history_deals_p = client_subs.add_parser("history_deals", help="Get MT5 history deals")
    history_deals_p.add_argument("--start", type=str, help="Start timestamp or datetime string")
    history_deals_p.add_argument("--end", type=str, help="End timestamp or datetime string")
    history_deals_p.add_argument("--group", type=str, help="MT5 group filter, e.g. *USD* or *, !EUR")
    history_deals_p.add_argument("--ticket", type=int, help="Filter by order ticket")
    history_deals_p.add_argument("--position", type=int, help="Filter by position ticket")

    # Order command
    order_p = client_subs.add_parser("order", help="Send a market order")
    order_p.add_argument("symbol", type=str)
    order_p.add_argument("type", type=str, choices=["BUY", "SELL"])
    order_p.add_argument("volume", type=float)
    order_p.add_argument("--sl", type=float, default=0.0)
    order_p.add_argument("--tp", type=float, default=0.0)
    order_p.add_argument("--comment", type=str, default="")
    order_p.add_argument("--magic", type=int, default=123456)

    # Close command
    close_p = client_subs.add_parser("close", help="Close a position")
    close_p.add_argument("ticket", type=int)

    # Modify command
    modify_p = client_subs.add_parser("modify", help="Modify position SL/TP")
    modify_p.add_argument("ticket", type=int)
    modify_p.add_argument("--sl", type=float, default=None)
    modify_p.add_argument("--tp", type=float, default=None)

    # Tick data commands (for tick scalping / high-frequency trading research)
    ticks_from_p = client_subs.add_parser("ticks_from", help="Get historical ticks from a specific date")
    ticks_from_p.add_argument("symbol", type=str)
    ticks_from_p.add_argument("--start", type=str, required=True, help="Start timestamp or datetime string (e.g. 2025-01-01)")
    ticks_from_p.add_argument("--count", type=int, default=1000, help="Number of ticks to retrieve")
    ticks_from_p.add_argument("--flags", type=str, default="ALL", choices=["ALL", "INFO", "TRADE"], 
                               help="Tick type: ALL, INFO (bid/ask), TRADE (last/volume)")
    
    ticks_range_p = client_subs.add_parser("ticks_range", help="Get historical ticks within a date range")
    ticks_range_p.add_argument("symbol", type=str)
    ticks_range_p.add_argument("--start", type=str, required=True, help="Start timestamp or datetime string")
    ticks_range_p.add_argument("--end", type=str, required=True, help="End timestamp or datetime string")
    ticks_range_p.add_argument("--flags", type=str, default="ALL", choices=["ALL", "INFO", "TRADE"],
                                help="Tick type: ALL, INFO (bid/ask), TRADE (last/volume)")

    # Market Book (Level 2) command
    book_p = client_subs.add_parser("book", help="Get current market depth (Level 2)")
    book_p.add_argument("symbol", type=str)

    args = parser.parse_args()

    if args.command == "server":
        if sys.platform != "win32":
            print("Error: Server functionality is only supported on Windows.")
            sys.exit(1)

        # Configure MT5 handler with CLI args
        if args.mt5_path:
            mt5_handler.program_path = args.mt5_path
        if args.mt5_login:
            mt5_handler.login = args.mt5_login
        if args.mt5_password:
            mt5_handler.password = args.mt5_password
        if args.mt5_server:
            mt5_handler.server = args.mt5_server
        
        # Configure UTC conversion
        mt5_handler.use_utc = not args.no_utc
        if mt5_handler.use_utc:
            print("UTC conversion enabled (Server Time -> UTC)")
        else:
            print("UTC conversion disabled (Raw Server Time)")

        # Run Server
        uvicorn.run(app, host=args.host, port=args.port)

    elif args.command == "client":
        client = BridgeClient(base_url=args.url)
        if args.client_command == "health":
            print(json.dumps(client.check_health(), indent=2))
        elif args.client_command == "rates":
            print(json.dumps(client.get_rates(args.symbol, args.timeframe, args.count), indent=2))
        elif args.client_command == "rates_range":
            try:
                start_ts = parse_datetime(args.start)
                end_ts = parse_datetime(args.end)
                print(json.dumps(client.get_rates_range(args.symbol, args.timeframe, start_ts, end_ts), indent=2))
            except Exception as e:
                print(f"Error parsing date: {e}")
                sys.exit(1)
        elif args.client_command == "tick":
            print(json.dumps(client.get_tick(args.symbol), indent=2))
        elif args.client_command == "account":
            print(json.dumps(client.get_account_info(), indent=2))
        elif args.client_command == "positions":
            symbols = args.symbols.split(",") if args.symbols else None
            print(json.dumps(client.get_positions(symbols=symbols, magic=args.magic), indent=2))
        elif args.client_command == "history_deals":
            try:
                # Convert date inputs only when range mode is used / 期間検索時のみ日付を変換
                start_ts = parse_datetime(args.start) if args.start else None
                end_ts = parse_datetime(args.end) if args.end else None
                result = client.get_history_deals(
                    start=start_ts,
                    end=end_ts,
                    group=args.group,
                    ticket=args.ticket,
                    position=args.position,
                )
                print(f"Retrieved {len(result)} deals")
                print(json.dumps(result[:10] if len(result) > 10 else result, indent=2))
                if len(result) > 10:
                    print(f"... and {len(result) - 10} more deals")
            except Exception as e:
                print(f"Error: {e}")
                sys.exit(1)
        elif args.client_command == "order":
            print(json.dumps(client.send_order(
                args.symbol, args.type, args.volume, args.sl, args.tp, args.comment, args.magic
            ), indent=2))
        elif args.client_command == "close":
            print(json.dumps(client.close_position(args.ticket), indent=2))
        elif args.client_command == "modify":
            print(json.dumps(client.modify_position(args.ticket, args.sl, args.tp), indent=2))
        elif args.client_command == "ticks_from":
            try:
                start_ts = parse_datetime(args.start)
                result = client.get_ticks_from(args.symbol, start_ts, args.count, args.flags)
                print(f"Retrieved {len(result)} ticks")
                print(json.dumps(result[:10] if len(result) > 10 else result, indent=2))
                if len(result) > 10:
                    print(f"... and {len(result) - 10} more ticks")
            except Exception as e:
                print(f"Error: {e}")
                sys.exit(1)
        elif args.client_command == "ticks_range":
            try:
                start_ts = parse_datetime(args.start)
                end_ts = parse_datetime(args.end)
                result = client.get_ticks_range(args.symbol, start_ts, end_ts, args.flags)
                print(f"Retrieved {len(result)} ticks")
                print(json.dumps(result[:10] if len(result) > 10 else result, indent=2))
                if len(result) > 10:
                    print(f"... and {len(result) - 10} more ticks")
            except Exception as e:
                print(f"Error: {e}")
                sys.exit(1)
        elif args.client_command == "book":
            print(json.dumps(client.get_book(args.symbol), indent=2))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
