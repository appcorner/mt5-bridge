import httpx
from typing import List, Dict, Optional, Any

class BridgeClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")

    def get_rates(self, symbol: str, timeframe: str = "M1", count: int = 1000) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/rates/{symbol}"
        params = {"timeframe": timeframe, "count": count}
        try:
            resp = httpx.get(url, params=params, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching rates: {e}")
            return []

    def get_rates_range(self, symbol: str, timeframe: str, start: int, end: int) -> List[Dict[str, Any]]:
        """
        Retrieve historical rates for a specified date range.

        Args:
            symbol: Symbol name, for example "XAUUSD"
            timeframe: Timeframe, for example "M1" or "H1"
            start: Start timestamp in UTC
            end: End timestamp in UTC

        Returns:
            A list of rate data dictionaries
        """
        url = f"{self.base_url}/rates_range/{symbol}"
        params = {"timeframe": timeframe, "start": start, "end": end}
        try:
            resp = httpx.get(url, params=params, timeout=30.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching rates range: {e}")
            return []

    def get_ticks_from(
        self,
        symbol: str,
        start: int,
        count: int = 1000,
        flags: str = "ALL"
    ) -> List[Dict[str, Any]]:
        """
        Retrieve historical tick data starting from the specified datetime.

        Args:
            symbol: Symbol name, for example "XAUUSD"
            start: Start timestamp in UTC
            count: Number of ticks to retrieve
            flags: Tick type, one of "ALL", "INFO", or "TRADE"

        Returns:
            A list of tick data dictionaries
        """
        url = f"{self.base_url}/ticks_from/{symbol}"
        params = {"start": start, "count": count, "flags": flags}
        try:
            # Use a longer timeout because tick responses can be large.
            resp = httpx.get(url, params=params, timeout=60.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching ticks from: {e}")
            return []

    def get_ticks_range(
        self,
        symbol: str,
        start: int,
        end: int,
        flags: str = "ALL"
    ) -> List[Dict[str, Any]]:
        """
        Retrieve historical tick data within the specified datetime range.

        Args:
            symbol: Symbol name, for example "XAUUSD"
            start: Start timestamp in UTC
            end: End timestamp in UTC
            flags: Tick type, one of "ALL", "INFO", or "TRADE"

        Returns:
            A list of tick data dictionaries
        """
        url = f"{self.base_url}/ticks_range/{symbol}"
        params = {"start": start, "end": end, "flags": flags}
        try:
            # Use a longer timeout because tick responses can be large.
            resp = httpx.get(url, params=params, timeout=120.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching ticks range: {e}")
            return []

    def get_tick(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/tick/{symbol}"
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching tick: {e}")
            return None

    def get_book(self, symbol: str) -> List[Dict[str, Any]]:
        """Get current market depth (Level 2)."""
        url = f"{self.base_url}/book/{symbol}"
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching market book: {e}")
            return []
    
    def get_account_info(self) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/account"
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching account info: {e}")
            return None
    
    def get_positions(self, symbols: Optional[List[str]] = None, magic: Optional[int] = None) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/positions"
        params = {}
        if symbols:
            params["symbols"] = ",".join(symbols)
        if magic is not None:
            params["magic"] = magic

        try:
            resp = httpx.get(url, params=params, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            print(f"Error fetching positions: {e}")
            return []
    
    def check_health(self) -> Dict[str, Any]:
        url = f"{self.base_url}/health"
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}

    def send_order(self, symbol: str, order_type: str, volume: float, sl: float = 0.0, tp: float = 0.0, comment: str = "", magic: int = 123456) -> Dict[str, Any]:
        url = f"{self.base_url}/order"
        data = {
            "symbol": symbol,
            "type": order_type,
            "volume": volume,
            "sl": sl,
            "tp": tp,
            "comment": comment,
            "magic": magic
        }
        try:
            resp = httpx.post(url, json=data, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}

    def close_position(self, ticket: int) -> Dict[str, Any]:
        url = f"{self.base_url}/close"
        data = {"ticket": ticket}
        try:
            resp = httpx.post(url, json=data, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}

    def modify_position(self, ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/modify"
        data = {
            "ticket": ticket,
            "sl": sl,
            "tp": tp,
            "update_sl": sl is not None,
            "update_tp": tp is not None
        }
        try:
            resp = httpx.post(url, json=data, timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}
