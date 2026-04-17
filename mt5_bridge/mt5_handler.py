import sys
import logging

# Configure logging / ตั้งค่าระบบบันทึก log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conditional import for MetaTrader5 / import MetaTrader5 แบบมีเงื่อนไขตามแพลตฟอร์ม
if sys.platform == 'win32':
    try:
        import MetaTrader5 as mt5
    except ImportError:
        mt5 = None
        logger.warning("MetaTrader5 package not found")
else:
    mt5 = None

import pandas as pd
from datetime import datetime, timezone
import logging
from typing import Optional, Dict, List, Union



class MT5Handler:
    def __init__(
        self,
        program_path: Optional[str] = None,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        use_utc: bool = True
    ):
        self.connected = False
        self.program_path = program_path
        self.login = login
        self.password = password
        self.server = server
        self.use_utc = use_utc
        self._server_offset_sec: Optional[int] = None

    def initialize(self) -> bool:
        """
        Initialize connection to MetaTrader 5 terminal.
        เริ่มต้นการเชื่อมต่อกับโปรแกรม MetaTrader 5.
        """
        # If path is specified, use it / ถ้ามีการระบุ path ให้ส่งต่อไปตอน initialize
        init_args = {}
        if self.program_path:
            init_args["path"] = self.program_path
            
        if mt5 is None:
            logger.error("MetaTrader5 is not available on this platform (Windows only).")
            self.connected = False
            return False

        if not mt5.initialize(**init_args):
            logger.error("initialize() failed, error code = %s", mt5.last_error())
            self.connected = False
            return False
            
        # If login credentials are provided, try to login /
        # ถ้ามีข้อมูลล็อกอินครบ ให้พยายาม login หลัง initialize
        if self.login and self.password and self.server:
            authorized = mt5.login(
                login=self.login,
                password=self.password,
                server=self.server
            )
            if not authorized:
                logger.error("failed to connect at account #%d, error code: %s", self.login, mt5.last_error())
                self.connected = False
                return False
            logger.info("MT5 login successful")

        logger.info("MT5 initialized successfully")
        self.connected = True
        return True

    def check_connection(self) -> bool:
        """
        Check if connection is still alive using terminal_info.
        Attempt to reconnect if lost.
        ตรวจสอบว่าการเชื่อมต่อยังใช้งานได้ผ่าน terminal_info.
        ถ้าหลุดการเชื่อมต่อให้ลองเชื่อมใหม่อัตโนมัติ.
        """
        if not mt5.terminal_info():
            self.connected = False
            logger.warning("Connection lost. Attempting to reconnect...")
            return self.initialize()
        
        self.connected = True
        return True

    def shutdown(self):
        """
        Shutdown connection to MetaTrader 5.
        ปิดการเชื่อมต่อกับ MetaTrader 5.
        """
        mt5.shutdown()
        self.connected = False
        logger.info("MT5 connection shutdown")

    def _update_server_offset(self, symbol: str):
        """
        Estimate server timezone offset relative to UTC using the given symbol's tick time.
        Offset = ServerTime - UTC.
        ประมาณค่า timezone offset ของ server เทียบกับ UTC จากเวลา tick ของ symbol ที่ระบุ.
        ค่า offset คำนวณจาก ServerTime - UTC.
        """
        if self._server_offset_sec is not None:
            return

        # Ensure symbol is selected to get fresh tick /
        # เลือก symbol ไว้ก่อนเพื่อเพิ่มโอกาสได้ tick ล่าสุด
        if not mt5.symbol_select(symbol, True):
            logger.warning(f"Failed to select symbol {symbol} for offset calculation")

        tick = mt5.symbol_info_tick(symbol)
        server_ts = 0
        
        if tick is not None:
            server_ts = int(tick.time)
        else:
            logger.warning(f"Tick not available for {symbol}, trying last rate for offset")
            # Try to fetch just 1 M1 bar to estimate server time /
            # ลองดึงแท่ง M1 เพียง 1 แท่งเพื่อประมาณเวลา server
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 1)
            if rates is not None and len(rates) > 0:
                server_ts = int(rates[0]['time'])
            else:
                logger.error(f"Could not determine server time for {symbol} (no tick, no rates)")
                return

        # Use the current UTC timestamp as the comparison baseline /
        # ใช้เวลา UTC ปัจจุบันเป็นฐานสำหรับเทียบ offset
        utc_ts = datetime.now(timezone.utc).timestamp()
        
        diff = server_ts - utc_ts
        # Round to the nearest 15 minutes to smooth latency and bar-close lag /
        # ปัดเป็นช่วงละ 15 นาทีเพื่อลดผลกระทบจาก latency และเวลาปิดแท่ง
        rounded_diff = round(diff / 900) * 900
        self._server_offset_sec = int(rounded_diff)
        logger.info(f"Server timezone offset estimated: {self._server_offset_sec}s (using {symbol}, raw_diff={diff:.1f}s)")

    def _apply_time_correction(self, ts: int) -> int:
        """
        Convert server timestamp to UTC if use_utc is True.
        แปลงเวลา server เป็น UTC เมื่อเปิดใช้ use_utc.
        """
        if not self.use_utc:
            return ts
        # If offset is not ready yet, fall back to zero correction for now /
        # ถ้ายังไม่มี offset ให้ถือว่าแก้ไขเวลาเป็นศูนย์ไปก่อนชั่วคราว
        # We rely on _update_server_offset being called before or during /
        # โดยคาดว่า _update_server_offset จะถูกเรียกก่อนหรือระหว่างการใช้งาน
        return int(ts - (self._server_offset_sec or 0))

    def _is_market_closed_from_tick(self, tick) -> bool:
        """
        Detect likely market-close state from symbol_info_tick() only.
        ประเมินอย่างระมัดระวังจาก symbol_info_tick() เพียงอย่างเดียวว่าตลาดน่าจะปิดอยู่หรือไม่.
        """
        # Guard against missing or malformed tick objects /
        # ถ้า tick หายหรือโครงสร้างผิดปกติ ให้โน้มเอียงไปทางปิดตลาด
        tick_time = int(getattr(tick, "time", 0) or 0)
        if tick_time <= 0:
            logger.warning("Tick time is missing or invalid. Treating as market closed.")
            return True

        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
        last = float(getattr(tick, "last", 0.0) or 0.0)
        volume = int(getattr(tick, "volume", 0) or 0)

        # Some brokers return an all-zero quote when the symbol is inactive /
        # บางโบรกเกอร์จะคืน quote เป็นศูนย์ทั้งหมดเมื่อ symbol ไม่มีการส่งราคา
        if bid == 0.0 and ask == 0.0 and last == 0.0 and volume == 0:
            logger.info("Market appears closed because tick quote is all zeros.")
            return True

        now_utc = datetime.now(timezone.utc)
        tick_utc = datetime.fromtimestamp(tick_time, tz=timezone.utc)
        tick_age_sec = (now_utc - tick_utc).total_seconds()

        # Weekend closure is common when MT5 keeps the last known quote /
        # ช่วงสุดสัปดาห์ MT5 มักคง quote ล่าสุดไว้แม้ตลาดปิดแล้ว
        if now_utc.weekday() >= 5 and tick_age_sec > 30 * 60:
            logger.info(
                "Market appears closed because the latest tick is stale on weekend: age=%.0fs",
                tick_age_sec,
            )
            return True

        # Keep the heuristic conservative on weekdays to avoid false positives /
        # ในวันทำการให้ใช้เกณฑ์แบบระวังเพื่อหลีกเลี่ยงการตัดสินผิดว่า market ปิด
        if tick_age_sec > 8 * 60 * 60 and volume == 0:
            logger.info(
                "Market appears closed because the latest tick is stale for %.0f seconds.",
                tick_age_sec,
            )
            return True

        return False

    def _map_deal_type(self, deal_type: int) -> str:
        """
        Return a readable MT5 deal type label.
        แปลง MT5 deal type ให้เป็นข้อความที่อ่านเข้าใจง่าย.
        """
        if mt5 is None:
            return str(deal_type)

        deal_type_map = {
            getattr(mt5, "DEAL_TYPE_BUY", -1): "BUY",
            getattr(mt5, "DEAL_TYPE_SELL", -2): "SELL",
            getattr(mt5, "DEAL_TYPE_BALANCE", -3): "BALANCE",
            getattr(mt5, "DEAL_TYPE_CREDIT", -4): "CREDIT",
            getattr(mt5, "DEAL_TYPE_CHARGE", -5): "CHARGE",
            getattr(mt5, "DEAL_TYPE_CORRECTION", -6): "CORRECTION",
            getattr(mt5, "DEAL_TYPE_BONUS", -7): "BONUS",
            getattr(mt5, "DEAL_TYPE_COMMISSION", -8): "COMMISSION",
            getattr(mt5, "DEAL_TYPE_COMMISSION_DAILY", -9): "COMMISSION_DAILY",
            getattr(mt5, "DEAL_TYPE_COMMISSION_MONTHLY", -10): "COMMISSION_MONTHLY",
            getattr(mt5, "DEAL_TYPE_COMMISSION_AGENT_DAILY", -11): "COMMISSION_AGENT_DAILY",
            getattr(mt5, "DEAL_TYPE_COMMISSION_AGENT_MONTHLY", -12): "COMMISSION_AGENT_MONTHLY",
            getattr(mt5, "DEAL_TYPE_INTEREST", -13): "INTEREST",
            getattr(mt5, "DEAL_TYPE_BUY_CANCELED", -14): "BUY_CANCELED",
            getattr(mt5, "DEAL_TYPE_SELL_CANCELED", -15): "SELL_CANCELED",
            getattr(mt5, "DEAL_TYPE_DIVIDEND", -16): "DIVIDEND",
            getattr(mt5, "DEAL_TYPE_DIVIDEND_FRANKED", -17): "DIVIDEND_FRANKED",
            getattr(mt5, "DEAL_TYPE_TAX", -18): "TAX",
        }
        return deal_type_map.get(int(deal_type), f"UNKNOWN_{deal_type}")

    def _map_deal_entry(self, entry_type: int) -> str:
        """
        Return a readable entry direction label.
        แปลงประเภท entry direction ให้เป็นข้อความที่อ่านง่าย.
        """
        if mt5 is None:
            return str(entry_type)

        entry_map = {
            getattr(mt5, "DEAL_ENTRY_IN", -1): "IN",
            getattr(mt5, "DEAL_ENTRY_OUT", -2): "OUT",
            getattr(mt5, "DEAL_ENTRY_INOUT", -3): "INOUT",
            getattr(mt5, "DEAL_ENTRY_OUT_BY", -4): "OUT_BY",
        }
        return entry_map.get(int(entry_type), f"UNKNOWN_{entry_type}")

    def _map_deal_reason(self, reason: int) -> str:
        """
        Return a readable reason label.
        แปลงเหตุผลของ deal ให้เป็นข้อความที่อ่านง่าย.
        """
        if mt5 is None:
            return str(reason)

        reason_map = {
            getattr(mt5, "DEAL_REASON_CLIENT", -1): "CLIENT",
            getattr(mt5, "DEAL_REASON_MOBILE", -2): "MOBILE",
            getattr(mt5, "DEAL_REASON_WEB", -3): "WEB",
            getattr(mt5, "DEAL_REASON_EXPERT", -4): "EXPERT",
            getattr(mt5, "DEAL_REASON_SL", -5): "SL",
            getattr(mt5, "DEAL_REASON_TP", -6): "TP",
            getattr(mt5, "DEAL_REASON_SO", -7): "SO",
            getattr(mt5, "DEAL_REASON_ROLLOVER", -8): "ROLLOVER",
            getattr(mt5, "DEAL_REASON_VMARGIN", -9): "VMARGIN",
            getattr(mt5, "DEAL_REASON_SPLIT", -10): "SPLIT",
            getattr(mt5, "DEAL_REASON_CORPORATE_ACTION", -11): "CORPORATE_ACTION",
        }
        return reason_map.get(int(reason), f"UNKNOWN_{reason}")

    def get_rates(self, symbol: str, timeframe_str: str, num_bars: int) -> Optional[List[Dict]]:
        """
        Get historical rates for a symbol.
        ดึงข้อมูลราคาในอดีตของ symbol.
        
        Args:
            symbol: Symbol name (e.g., "XAUUSD")
            timeframe_str: Timeframe string (e.g., "M1", "H1")
            num_bars: Number of bars to fetch
            
        Returns:
            List of dictionaries containing rate data, or None if failed.
            คืนรายการข้อมูลราคาแบบ dictionary หรือ None ถ้าล้มเหลว.
        """
        if not self.connected:
            if not self.initialize():
                return None

        # Map timeframe string to the MT5 constant /
        # แปลงชื่อ timeframe จากข้อความให้เป็นค่าคงที่ของ MT5
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
            "W1": getattr(mt5, "TIMEFRAME_W1", None),
            "MN1": getattr(mt5, "TIMEFRAME_MN1", None),
        }
        
        mt5_tf = tf_map.get(timeframe_str)
        if mt5_tf is None:
            logger.error(f"Invalid timeframe: {timeframe_str}")
            return None

        # Copy rates starting from the latest bar backward /
        # ดึงข้อมูลแท่งจากปัจจุบันย้อนหลังไปตามจำนวนที่ต้องการ
        rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, num_bars)
        
        if self.use_utc and self._server_offset_sec is None:
            self._update_server_offset(symbol)
        
        if rates is None:
            logger.error(f"Failed to get rates for {symbol}")
            return None
            
        # Convert the NumPy record array into plain dictionaries /
        # แปลง NumPy record array ให้อยู่ในรูป dictionary ปกติ
        result = []
        for rate in rates:
            result.append({
                "time": self._apply_time_correction(int(rate['time'])),
                "open": float(rate['open']),
                "high": float(rate['high']),
                "low": float(rate['low']),
                "close": float(rate['close']),
                "tick_volume": int(rate['tick_volume']),
                "spread": int(rate['spread']),
                "real_volume": int(rate['real_volume'])
            })
            
        return result

    def get_rates_range(self, symbol: str, timeframe_str: str, date_from: datetime, date_to: datetime) -> Optional[List[Dict]]:
        """
        Get historical rates for a symbol within a date range.
        ดึงข้อมูลราคาในอดีตของ symbol ภายในช่วงวันที่กำหนด.
        
        Args:
            symbol: Symbol name
            timeframe_str: Timeframe string
            date_from: Start date (datetime)
            date_to: End date (datetime)
            
        Returns:
            List of dictionaries containing rate data.
            คืนรายการข้อมูลราคาแบบ dictionary.
        """
        if not self.connected:
            if not self.initialize():
                return None

        # Map timeframe string to the MT5 constant /
        # แปลงชื่อ timeframe จากข้อความให้เป็นค่าคงที่ของ MT5
        tf_map = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
            "W1": getattr(mt5, "TIMEFRAME_W1", None),
            "MN1": getattr(mt5, "TIMEFRAME_MN1", None),
        }
        
        mt5_tf = tf_map.get(timeframe_str)
        if mt5_tf is None:
            logger.error(f"Invalid timeframe: {timeframe_str}")
            return None

        # copy_rates_range expects server time, so reverse UTC conversion here /
        # copy_rates_range ต้องใช้เวลา server จึงต้องแปลงย้อนจาก UTC ตรงนี้
        if self.use_utc and self._server_offset_sec is None:
            self._update_server_offset(symbol)
        
        offset = self._server_offset_sec or 0
        server_from = datetime.fromtimestamp(date_from.timestamp() + offset)
        server_to = datetime.fromtimestamp(date_to.timestamp() + offset)

        rates = mt5.copy_rates_range(symbol, mt5_tf, server_from, server_to)
        
        if rates is None:
            logger.error(f"Failed to get rates range for {symbol} ({mt5.last_error()})")
            return None
            
        result = []
        for rate in rates:
            result.append({
                "time": self._apply_time_correction(int(rate['time'])),
                "open": float(rate['open']),
                "high": float(rate['high']),
                "low": float(rate['low']),
                "close": float(rate['close']),
                "tick_volume": int(rate['tick_volume']),
                "spread": int(rate['spread']),
                "real_volume": int(rate['real_volume'])
            })
            
        return result

    def get_tick(self, symbol: str) -> Optional[Dict]:
        """
        Get latest tick data.
        Return None when the symbol appears to be in a market-close state.
        ดึง tick ล่าสุด และคืน None ถ้า symbol ดูเหมือนอยู่ในช่วงตลาดปิด.
        """
        if not self.connected:
            if not self.initialize():
                return None
                
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error(f"Failed to get tick for {symbol}")
            return None

        # MT5 may keep returning the last known tick while the market is closed /
        # MT5 อาจยังคืน tick ล่าสุดเดิมแม้ตลาดปิด จึงต้องมีการกรองเพิ่ม
        if self._is_market_closed_from_tick(tick):
            logger.info(f"Market is closed for {symbol}. Returning None instead of stale tick.")
            return None
            
        if self.use_utc and self._server_offset_sec is None:
            self._update_server_offset(symbol)

        return {
            "time": self._apply_time_correction(int(tick.time)),
            "time_msc": int(tick.time_msc),
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "last": float(tick.last),
            "volume": int(tick.volume)
        }

    def get_ticks_from(
        self,
        symbol: str,
        date_from: datetime,
        count: int,
        flags: str = "ALL"
    ) -> Optional[List[Dict]]:
        """
        Retrieve a specified number of historical ticks starting from a given datetime.
        ดึง historical ticks ตามจำนวนที่ต้องการ โดยเริ่มจากเวลาที่ระบุ.

        Args:
            symbol: Symbol name, for example "XAUUSD"
            date_from: Start datetime, preferably in UTC
            count: Number of ticks to retrieve
            flags: Tick type ("ALL", "INFO", "TRADE")
                   - ALL: all ticks
                   - INFO: ticks with Bid/Ask changes
                   - TRADE: ticks with Last/Volume changes

        Returns:
            A list of tick data dictionaries, or None if an error occurs
            คืนรายการ tick แบบ dictionary หรือ None เมื่อเกิดข้อผิดพลาด
        """
        if not self.connected:
            if not self.initialize():
                return None
        
        # Map public flag names to MT5 constants /
        # แปลงชื่อ flags ที่รับจากภายนอกเป็นค่าคงที่ของ MT5
        flag_map = {
            "ALL": mt5.COPY_TICKS_ALL,
            "INFO": mt5.COPY_TICKS_INFO,
            "TRADE": mt5.COPY_TICKS_TRADE,
        }
        mt5_flags = flag_map.get(flags.upper(), mt5.COPY_TICKS_ALL)
        
        # Convert the requested UTC timestamp back to server time /
        # แปลงเวลาที่รับมาใน UTC กลับเป็นเวลา server ก่อนเรียก MT5
        if self.use_utc and self._server_offset_sec is None:
            self._update_server_offset(symbol)
        
        offset = self._server_offset_sec or 0
        server_from = datetime.fromtimestamp(date_from.timestamp() + offset, tz=timezone.utc)
        
        ticks = mt5.copy_ticks_from(symbol, server_from, count, mt5_flags)
        
        if ticks is None:
            logger.error(f"Failed to get ticks from {symbol}: {mt5.last_error()}")
            return None
        
        if len(ticks) == 0:
            logger.warning(f"No ticks returned for {symbol} from {date_from}")
            return []
        
        # Convert the NumPy array into plain dictionaries /
        # แปลงผลลัพธ์ NumPy array ให้อยู่ในรูป dictionary ปกติ
        result = []
        for tick in ticks:
            result.append({
                "time": self._apply_time_correction(int(tick['time'])),
                "time_msc": int(tick['time_msc']),  # Timestamp with millisecond precision / timestamp ระดับมิลลิวินาที
                "bid": float(tick['bid']),
                "ask": float(tick['ask']),
                "last": float(tick['last']),
                "volume": int(tick['volume']),
                "flags": int(tick['flags']),  # Tick change flags / สถานะการเปลี่ยนแปลงของ tick
            })
        
        return result

    def get_ticks_range(
        self,
        symbol: str,
        date_from: datetime,
        date_to: datetime,
        flags: str = "ALL"
    ) -> Optional[List[Dict]]:
        """
        Retrieve historical tick data within the specified datetime range.
        ดึง historical tick ภายในช่วงเวลาที่กำหนด.

        Args:
            symbol: Symbol name, for example "XAUUSD"
            date_from: Start datetime, preferably in UTC
            date_to: End datetime, preferably in UTC
            flags: Tick type ("ALL", "INFO", "TRADE")

        Returns:
            A list of tick data dictionaries, or None if an error occurs
            คืนรายการ tick แบบ dictionary หรือ None เมื่อเกิดข้อผิดพลาด
        """
        if not self.connected:
            if not self.initialize():
                return None
        
        # Map public flag names to MT5 constants /
        # แปลงชื่อ flags ที่รับจากภายนอกเป็นค่าคงที่ของ MT5
        flag_map = {
            "ALL": mt5.COPY_TICKS_ALL,
            "INFO": mt5.COPY_TICKS_INFO,
            "TRADE": mt5.COPY_TICKS_TRADE,
        }
        mt5_flags = flag_map.get(flags.upper(), mt5.COPY_TICKS_ALL)
        
        # Convert the requested UTC range back to server time /
        # แปลงช่วงเวลาที่รับมาใน UTC กลับเป็นเวลา server ก่อนเรียก MT5
        if self.use_utc and self._server_offset_sec is None:
            self._update_server_offset(symbol)
        
        offset = self._server_offset_sec or 0
        server_from = datetime.fromtimestamp(date_from.timestamp() + offset, tz=timezone.utc)
        server_to = datetime.fromtimestamp(date_to.timestamp() + offset, tz=timezone.utc)
        
        ticks = mt5.copy_ticks_range(symbol, server_from, server_to, mt5_flags)
        
        if ticks is None:
            logger.error(f"Failed to get ticks range for {symbol}: {mt5.last_error()}")
            return None
        
        if len(ticks) == 0:
            logger.warning(f"No ticks returned for {symbol} in range {date_from} to {date_to}")
            return []
        
        # Convert the NumPy array into plain dictionaries /
        # แปลงผลลัพธ์ NumPy array ให้อยู่ในรูป dictionary ปกติ
        result = []
        for tick in ticks:
            result.append({
                "time": self._apply_time_correction(int(tick['time'])),
                "time_msc": int(tick['time_msc']),  # Timestamp with millisecond precision / timestamp ระดับมิลลิวินาที
                "bid": float(tick['bid']),
                "ask": float(tick['ask']),
                "last": float(tick['last']),
                "volume": int(tick['volume']),
                "flags": int(tick['flags']),  # Tick change flags / สถานะการเปลี่ยนแปลงของ tick
            })
        
        return result

    def get_history_deals(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        group: Optional[str] = None,
        ticket: Optional[int] = None,
        position: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """
        Retrieve trading history deals using the same lookup modes as
                MetaTrader5.history_deals_get.
                ดึงประวัติ deals โดยรองรับรูปแบบการค้นหาแบบเดียวกับ MetaTrader5.history_deals_get.

        Notes:
        - Direct ticket/position lookups may fail with some brokers or terminal builds.
        - When that happens, fall back to a wide range scan and filter locally.
                - ถ้าการค้นหาแบบ ticket/position โดยตรงล้มเหลว จะ fallback ไปสแกนช่วงกว้างแล้วค่อยกรองในเครื่อง.
        """
        if not self.connected:
            if not self.initialize():
                return None

        def _range_query(start_dt: datetime, end_dt: datetime, group_filter: Optional[str] = None):
            """
            Run a range-based MT5 history query.
            เรียกค้นประวัติ MT5 แบบระบุช่วงเวลา.
            """
            if group_filter:
                return mt5.history_deals_get(start_dt, end_dt, group=group_filter)
            return mt5.history_deals_get(start_dt, end_dt)

        def _fallback_scan(filter_kind: str, filter_value: int):
            """
            Some MT5 terminals reject direct lookup params with last_error=(-2, Invalid params).
            In that case, scan a wide history range and filter locally.
            บาง terminal ของ MT5 จะปฏิเสธพารามิเตอร์ค้นหาแบบตรงด้วย last_error=(-2, Invalid params)
            ในกรณีนั้นให้ดึงช่วงประวัติกว้าง ๆ แล้วกรองที่ฝั่ง local แทน.
            """
            fallback_from = datetime(2000, 1, 1, tzinfo=timezone.utc)
            fallback_to = datetime.now(timezone.utc)
            scanned = _range_query(fallback_from, fallback_to)
            if scanned is None:
                return None

            if filter_kind == "ticket":
                # Accept both deal ticket and order ticket for convenience /
                # เพื่อความสะดวก ให้ยอมรับทั้ง deal ticket และ order ticket
                return [
                    deal for deal in scanned
                    if int(getattr(deal, "ticket", 0)) == filter_value
                    or int(getattr(deal, "order", 0)) == filter_value
                ]

            if filter_kind == "position":
                return [
                    deal for deal in scanned
                    if int(getattr(deal, "position_id", 0)) == filter_value
                ]

            return scanned

        try:
            # Select exactly one query mode /
            # บังคับให้เลือกโหมดค้นหาเพียงแบบเดียวในแต่ละครั้ง
            if ticket is not None:
                ticket_value = int(ticket)
                deals = mt5.history_deals_get(ticket=ticket_value)
                if deals is None:
                    logger.warning(
                        "Direct history_deals_get(ticket=%s) failed: %s. Falling back to range scan.",
                        ticket_value,
                        mt5.last_error(),
                    )
                    deals = _fallback_scan("ticket", ticket_value)
            elif position is not None:
                position_value = int(position)
                deals = mt5.history_deals_get(position=position_value)
                if deals is None:
                    logger.warning(
                        "Direct history_deals_get(position=%s) failed: %s. Falling back to range scan.",
                        position_value,
                        mt5.last_error(),
                    )
                    deals = _fallback_scan("position", position_value)
            else:
                if date_from is None or date_to is None:
                    logger.error("date_from and date_to are required when ticket/position is not provided")
                    return None

                # The group filter is only meaningful for range queries /
                # group filter ใช้เฉพาะตอนค้นหาตามช่วงเวลาเท่านั้น
                deals = _range_query(date_from, date_to, group)
        except Exception as exc:
            logger.error(f"Failed to query history deals: {exc}")
            return None

        if deals is None:
            logger.error(f"Failed to get history deals: {mt5.last_error()}")
            return None

        if len(deals) == 0:
            return []

        # Estimate server offset from the first returned symbol when needed /
        # หากจำเป็น ให้ประมาณค่า server offset จาก symbol แรกที่ได้กลับมา
        if self.use_utc and self._server_offset_sec is None:
            first_symbol = getattr(deals[0], "symbol", None)
            if first_symbol:
                self._update_server_offset(first_symbol)

        result = []
        for deal in deals:
            result.append({
                "ticket": int(getattr(deal, "ticket", 0)),
                "order": int(getattr(deal, "order", 0)),
                "time": self._apply_time_correction(int(getattr(deal, "time", 0))),
                "time_msc": int(getattr(deal, "time_msc", 0)),
                "type": self._map_deal_type(int(getattr(deal, "type", -1))),
                "type_code": int(getattr(deal, "type", -1)),
                "entry": self._map_deal_entry(int(getattr(deal, "entry", -1))),
                "entry_code": int(getattr(deal, "entry", -1)),
                "magic": int(getattr(deal, "magic", 0)),
                "position_id": int(getattr(deal, "position_id", 0)),
                "reason": self._map_deal_reason(int(getattr(deal, "reason", -1))),
                "reason_code": int(getattr(deal, "reason", -1)),
                "volume": float(getattr(deal, "volume", 0.0) or 0.0),
                "price": float(getattr(deal, "price", 0.0) or 0.0),
                "commission": float(getattr(deal, "commission", 0.0) or 0.0),
                "swap": float(getattr(deal, "swap", 0.0) or 0.0),
                "profit": float(getattr(deal, "profit", 0.0) or 0.0),
                "fee": float(getattr(deal, "fee", 0.0) or 0.0),
                "symbol": str(getattr(deal, "symbol", "")),
                "comment": str(getattr(deal, "comment", "")),
                "external_id": str(getattr(deal, "external_id", "")),
            })

        # Return oldest -> newest for stable downstream processing /
        # คืนค่าจากเก่าไปใหม่เพื่อให้ downstream ประมวลผลได้สม่ำเสมอ
        result.sort(key=lambda item: (item["time"], item["time_msc"], item["ticket"]))
        return result

    def get_account_info(self) -> Optional[Dict]:
        """
        Get account information (balance, equity, etc.).
        ดึงข้อมูลบัญชี เช่น balance, equity และค่า margin ต่าง ๆ.
        """
        if not self.connected:
            if not self.initialize():
                return None
                
        account_info = mt5.account_info()
        if account_info is None:
            logger.error(f"Failed to get account info: {mt5.last_error()}")
            return None
            
        return {
            "login": int(account_info.login),
            "balance": float(account_info.balance),
            "equity": float(account_info.equity),
            "margin": float(account_info.margin),
            "margin_free": float(account_info.margin_free),
            "margin_level": float(account_info.margin_level),
            "leverage": int(account_info.leverage),
            "currency": str(account_info.currency),
            "server": str(account_info.server)
        }

    def get_positions(
        self,
        symbols: Optional[List[str]] = None,
        magic: Optional[int] = None,
    ) -> Optional[List[Dict]]:
        """
        Get current open positions with optional filtering.
        ดึงรายการ position ที่เปิดอยู่ โดยสามารถกรองเพิ่มเติมได้.
        
        Args:
            symbols: If provided, only return positions for these symbols.
            magic: If provided, only return positions with this magic number.
        
        Returns:
            List of position dictionaries, or None if failed.
            คืนรายการ position แบบ dictionary หรือ None ถ้าล้มเหลว.
        """
        if not self.connected:
            if not self.initialize():
                return None
                
        positions = mt5.positions_get()
        if positions is None:
            return []
            
        result = []
        for pos in positions:
            # Apply the magic number filter /
            # กรองตาม magic number เมื่อผู้ใช้ระบุมา
            pos_magic = int(getattr(pos, "magic", 0))
            if magic is not None and pos_magic != magic:
                continue
            
            # Apply the symbol filter /
            # กรองตาม symbol เมื่อผู้ใช้ส่งรายการ symbol มา
            pos_symbol = pos.symbol
            if symbols is not None and pos_symbol not in symbols:
                continue
            
            result.append({
                "ticket": int(pos.ticket),
                "symbol": pos_symbol,
                "type": "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume": float(pos.volume),
                "price_open": float(pos.price_open),
                # Preserve the original comment for downstream identification /
                # เก็บ comment เดิมไว้เพื่อให้ระบบปลายทางใช้ระบุตำแหน่งของตัวเองได้
                "comment": str(getattr(pos, "comment", "")),
                "magic": pos_magic,
                "sl": float(pos.sl),
                "tp": float(pos.tp),
                "price_current": float(pos.price_current),
                "profit": float(pos.profit),
                "time": self._apply_time_correction(int(pos.time)),
                "time_msc": int(getattr(pos, "time_msc", 0))
            })
            
        return result

    def send_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        sl: float = 0.0,
        tp: float = 0.0,
        comment: str = "",
        magic: int = 123456,
    ) -> tuple[Optional[int], Optional[str]]:
        """
        Send a market order.
        ส่งคำสั่ง market order ไปยัง MT5.
        
        Args:
            symbol: Symbol to trade.
            order_type: "BUY" or "SELL".
            volume: Lot size.
            sl: Stop Loss price.
            tp: Take Profit price.
            comment: Order comment.
            
        Returns:
            Order ticket if successful, None otherwise.
            คืน order ticket เมื่อสำเร็จ หรือ None เมื่อไม่สำเร็จ.
        """
        if not self.connected:
            if not self.initialize():
                message = "Failed to connect to MT5"
                return None, message
                
        # Get the current price needed for the order request /
        # ดึงราคาปัจจุบันเพื่อใช้ประกอบคำสั่งซื้อขาย
        tick = self.get_tick(symbol)
        if tick is None:
            message = f"Failed to get tick data for {symbol}"
            logger.error(message)
            return None, message
            
        action = mt5.TRADE_ACTION_DEAL
        mt5_type = mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick['ask'] if order_type == "BUY" else tick['bid']
        
        base_request = {
            "action": action,
            "symbol": symbol,
            "volume": volume,
            "type": mt5_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,  # Slippage tolerance / ค่าความคลาดเคลื่อนที่ยอมรับได้
            "magic": magic,   # Magic number / หมายเลขสำหรับแยกกลุ่มคำสั่ง
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
        }

        # Only switch filling mode when the failure clearly points to filling policy /
        # เปลี่ยน filling mode เฉพาะเมื่อสาเหตุล้มเหลวเกี่ยวกับ filling policy จริง ๆ
        # For example, Invalid stops will not be fixed by trying every filling option /
        # เช่น Invalid stops จะไม่หายเพียงแค่ลอง filling mode ทุกแบบ
        invalid_fill_retcode = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)

        # Try the default request first, then retry alternative filling modes only when relevant /
        # ลองคำสั่งแบบปกติก่อน แล้วค่อย fallback ไป filling mode อื่นเฉพาะกรณีที่เกี่ยวข้อง
        fillings = [
            None,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]
        last_error: Optional[str] = None
        for filling in fillings:
            request = dict(base_request)
            if filling is not None:
                request["type_filling"] = filling
            filling_label = "default" if filling is None else str(filling)
            result = mt5.order_send(request)
            if result is None:
                # When result is None, the problem is often terminal or transport related /
                # ถ้า result เป็น None มักเป็นปัญหาจาก terminal หรือการสื่อสาร ไม่ใช่ filling mode
                error_code = mt5.last_error()
                last_error = f"order_send returned None with filling={filling_label} (error={error_code}). Request: {request}"
                logger.error(last_error)
                break
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"Order sent successfully: {result.order} (filling={filling_label})")
                return result.order, None
            last_error = f"filling={filling_label} failed with retcode {result.retcode}: {result.comment}"
            logger.warning("Order send failed: %s", last_error)

            # Retry the next filling mode only for unsupported or invalid filling errors /
            # ลอง filling mode ถัดไปเฉพาะกรณี unsupported หรือ invalid filling เท่านั้น
            # For other failures, stop immediately and return /
            # หากเป็นความผิดพลาดชนิดอื่นให้หยุดและคืนผลทันที
            if int(result.retcode) == int(invalid_fill_retcode) or "filling" in str(result.comment).lower():
                continue
            break
        message = last_error or "Order placement failed for all tested filling modes"
        return None, message

    def close_position(self, ticket: int) -> tuple[bool, str]:
        """
        Close an existing position.
        Returns: (success, message)
        ปิด position ที่เปิดอยู่ และคืนค่าเป็น (success, message).
        """
        if not self.connected:
            if not self.initialize():
                return False, "Failed to connect to MT5"
                
        # Read position details first so the close order can mirror symbol and volume /
        # อ่านรายละเอียด position ก่อน เพื่อใช้ symbol และ volume เดิมตอนปิดคำสั่ง
        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            logger.error(f"Position {ticket} not found")
            return False, f"Position {ticket} not found"
            
        pos = positions[0]
        symbol = pos.symbol
        volume = pos.volume
        
        # Use the opposite side to close the position /
        # ใช้ฝั่งตรงข้ามกับ position เดิมเพื่อปิดคำสั่ง
        order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        
        # Get the current executable price for the close request /
        # ดึงราคาปัจจุบันที่ใช้ส่งคำสั่งปิดได้จริง
        tick = self.get_tick(symbol)
        if tick is None:
            return False, f"Failed to get tick for {symbol}"
            
        price = tick['bid'] if order_type == mt5.ORDER_TYPE_SELL else tick['ask']
        
        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "position": ticket,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "Close position",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        # Only switch filling mode when the failure is caused by filling policy /
        # เปลี่ยน filling mode เฉพาะเมื่อความล้มเหลวเกิดจาก filling policy
        invalid_fill_retcode = getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)

        fillings = [
            None,
            mt5.ORDER_FILLING_IOC,
            mt5.ORDER_FILLING_FOK,
            mt5.ORDER_FILLING_RETURN,
        ]
        last_error: Optional[str] = None
        for filling in fillings:
            request = dict(base_request)
            if filling is not None:
                request["type_filling"] = filling
            filling_label = "default" if filling is None else str(filling)
            result = mt5.order_send(request)
            if result is None:
                error_code = mt5.last_error()
                last_error = f"order_send returned None with filling={filling_label} (error={error_code})"
                logger.error(last_error)
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info("Position %s closed successfully (filling=%s)", ticket, filling_label)
                return True, "Success"
            last_error = f"filling={filling_label} failed with retcode {result.retcode}: {result.comment}"
            logger.warning("Close position failed: %s", last_error)

            # Retry the next filling mode only for unsupported or invalid filling errors /
            # ลอง filling mode ถัดไปเฉพาะกรณี unsupported หรือ invalid filling เท่านั้น
            # For other failures, stop immediately and return /
            # หากเป็นความผิดพลาดชนิดอื่นให้หยุดและคืนผลทันที
            if int(result.retcode) == int(invalid_fill_retcode) or "filling" in str(result.comment).lower():
                continue
            break

        message = last_error or "Close position failed"
        return False, message

    def modify_position(self, ticket: int, sl: Optional[float], tp: Optional[float], update_sl: bool, update_tp: bool) -> tuple[bool, str]:
        """
        Adjust stop loss / take profit for an existing position.
        ปรับ stop loss และ take profit ของ position ที่มีอยู่.
        """

        if not update_sl and not update_tp:
            return False, "Nothing to update"

        if not self.connected:
            if not self.initialize():
                return False, "Failed to connect to MT5"

        positions = mt5.positions_get(ticket=ticket)
        if positions is None or len(positions) == 0:
            logger.error(f"Position {ticket} not found")
            return False, f"Position {ticket} not found"

        pos = positions[0]
        symbol = pos.symbol
        action = getattr(mt5, "TRADE_ACTION_SLTP", None)
        if action is None:
            logger.error("MT5 does not support TRADE_ACTION_SLTP")
            return False, "TRADE_ACTION_SLTP not available"

        sl_value = float(pos.sl or 0.0)
        tp_value = float(pos.tp or 0.0)

        if update_sl:
            sl_value = 0.0 if sl is None else float(sl)

        if update_tp:
            tp_value = 0.0 if tp is None else float(tp)

        request = {
            "action": action,
            "position": ticket,
            "symbol": symbol,
            "sl": sl_value,
            "tp": tp_value,
        }

        result = mt5.order_send(request)
        if result is None:
            logger.error("Modify position failed: result is None")
            return False, "order_send returned None"

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = f"{result.comment} ({result.retcode})"
            logger.error(f"Modify position failed: {error_msg}")
            return False, error_msg

        logger.info(f"Protection updated for ticket {ticket}")
        return True, "Success"

    def get_market_book(self, symbol: str) -> Optional[List[Dict]]:
        """
        Get market depth (Level 2) data for a symbol.
        Automatically handles subscription.
        ดึงข้อมูล market depth (Level 2) ของ symbol และจัดการ subscription ให้อัตโนมัติ.
        """
        if not self.connected:
            if not self.initialize():
                return None

        # Ensure the symbol is selected before subscribing to market book /
        # เลือก symbol ให้พร้อมก่อนสมัครรับ market book
        if not mt5.symbol_select(symbol, True):
            logger.error(f"Failed to select symbol {symbol}")
            return None

        # Subscribe to market book before calling MarketBookGet /
        # ต้อง subscribe market book ก่อนจึงจะเรียก MarketBookGet ได้
        if not mt5.market_book_add(symbol):
            logger.error(f"Failed to subscribe to market book for {symbol}: {mt5.last_error()}")
            return None

        # Retrieve the market book snapshot /
        # ดึง snapshot ของ market book ปัจจุบัน
        items = mt5.market_book_get(symbol)
        if items is None:
            # Note: the terminal may return None before the order book is populated /
            # หมายเหตุ: terminal อาจคืน None ได้ถ้า order book ยังไม่พร้อม
            return []

        result = []
        for item in items:
            result.append({
                "type": "BUY" if item.type == mt5.BOOK_TYPE_BUY else 
                        "SELL" if item.type == mt5.BOOK_TYPE_SELL else 
                        "BUY_LIMIT" if item.type == mt5.BOOK_TYPE_BUY_LIMIT else
                        "SELL_LIMIT" if item.type == mt5.BOOK_TYPE_SELL_LIMIT else "OTHER",
                "price": float(item.price),
                "volume": float(item.volume),
                # "volume_real": float(item.volume_real)  # Optional raw volume field / field ปริมาณแบบ raw ที่เปิดใช้ได้ถ้าต้องการ
                "volume_dbl": float(item.volume_dbl)
            })
        
        return result
