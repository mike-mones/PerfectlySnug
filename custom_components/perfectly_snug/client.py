"""WebSocket client for communicating with the Perfectly Snug Smart Topper."""

import asyncio
import logging
import struct
from typing import Any

import websockets

from .const import (
    CTRL_CMD_GET_SETTINGS,
    CTRL_CMD_SET_SETTING,
    CTRL_MSG_SETTING,
    CTRL_MSG_SETTINGS,
    MSG_GROUP_CTRL,
    POLL_SETTINGS,
    TEMP_SETTINGS,
    WS_ENDPOINT,
    WS_ORIGIN,
)

_LOGGER = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds, doubles each attempt


def raw_to_celsius(raw: int) -> float:
    """Convert raw sensor value to Celsius. Only valid for TA, TSR, TSC, TSL, TempSP."""
    return round((raw - 32768) / 100, 2)


def raw_to_fahrenheit(raw: int) -> float:
    """Convert raw sensor value to Fahrenheit."""
    return round(raw_to_celsius(raw) * 9 / 5 + 32, 1)


class TopperClient:
    """WebSocket client for a single Perfectly Snug topper zone."""

    def __init__(self, ip: str) -> None:
        """Initialize the client."""
        self.ip = ip
        self.url = f"ws://{ip}{WS_ENDPOINT}"
        self._tx_id = 0

    def _next_tx_id(self) -> int:
        """Get next transaction ID."""
        self._tx_id = 1 if self._tx_id >= 65535 else self._tx_id + 1
        return self._tx_id

    def _build_get_settings(self, setting_ids: list[int]) -> bytes:
        """Build a GET_SETTINGS command."""
        payload = b""
        for sid in setting_ids:
            payload += struct.pack(">H", sid)
        header = struct.pack(
            ">BHHH",
            MSG_GROUP_CTRL,
            CTRL_CMD_GET_SETTINGS,
            self._next_tx_id(),
            len(payload),
        )
        return header + payload

    def _build_set_setting(self, setting_id: int, value: int) -> bytes:
        """Build a SET_SETTING command."""
        payload = struct.pack(">HH", setting_id, value)
        header = struct.pack(
            ">BHHH",
            MSG_GROUP_CTRL,
            CTRL_CMD_SET_SETTING,
            self._next_tx_id(),
            len(payload),
        )
        return header + payload

    @staticmethod
    def _parse_response(data: bytes) -> dict[int, int]:
        """Parse binary message into {setting_id: value} dict."""
        readings: dict[int, int] = {}
        if len(data) < 7:
            return readings
        group = data[0]
        cmd_id = (data[1] << 8) | data[2]
        payload = data[7:]
        if group == MSG_GROUP_CTRL and cmd_id in (CTRL_MSG_SETTING, CTRL_MSG_SETTINGS):
            for i in range(0, len(payload), 4):
                if i + 4 <= len(payload):
                    sid = (payload[i] << 8) | payload[i + 1]
                    val = (payload[i + 2] << 8) | payload[i + 3]
                    readings[sid] = val
        return readings

    async def async_test_connection(self) -> bool:
        """Test if we can connect to the topper."""
        try:
            async with websockets.connect(
                self.url,
                origin=WS_ORIGIN,
                ping_interval=None,
                close_timeout=5,
                open_timeout=5,
            ):
                return True
        except Exception:
            _LOGGER.debug("Connection test failed for %s", self.ip)
            return False

    async def async_get_settings(
        self, setting_ids: list[int] | None = None
    ) -> dict[int, int]:
        """Fetch settings from the topper with retry on failure."""
        if setting_ids is None:
            setting_ids = POLL_SETTINGS

        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self._get_settings_once(setting_ids)
            except Exception as err:
                last_err = err
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    _LOGGER.debug(
                        "Retry %d/%d for %s in %.1fs: %s",
                        attempt + 1, MAX_RETRIES, self.ip, delay, err,
                    )
                    await asyncio.sleep(delay)

        _LOGGER.error("Failed to get settings from %s after %d attempts: %s",
                       self.ip, MAX_RETRIES, last_err)
        raise ConnectionError(f"Cannot reach topper at {self.ip}") from last_err

    async def _get_settings_once(self, setting_ids: list[int]) -> dict[int, int]:
        """Single attempt to fetch settings."""
        readings: dict[int, int] = {}
        loop = asyncio.get_running_loop()
        async with websockets.connect(
            self.url,
            origin=WS_ORIGIN,
            ping_interval=None,
            close_timeout=5,
            open_timeout=5,
        ) as ws:
            for i in range(0, len(setting_ids), 8):
                batch = setting_ids[i : i + 8]
                await ws.send(self._build_get_settings(batch))
                await asyncio.sleep(0.2)

            deadline = loop.time() + 10
            while loop.time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    if isinstance(msg, bytes):
                        readings.update(self._parse_response(msg))
                    if len(readings) >= len(setting_ids):
                        break
                except asyncio.TimeoutError:
                    break

        return readings

    async def async_set_setting(self, setting_id: int, value: int) -> dict[int, int]:
        """Set a single setting on the topper. Returns confirmed values from device.

        Raises ConnectionError on failure after retries (instead of silently
        returning False).
        """
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self._set_setting_once(setting_id, value)
            except Exception as err:
                last_err = err
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    _LOGGER.debug(
                        "Retry set %d=%d on %s (%d/%d) in %.1fs: %s",
                        setting_id, value, self.ip,
                        attempt + 1, MAX_RETRIES, delay, err,
                    )
                    await asyncio.sleep(delay)

        _LOGGER.error("Failed to set setting %d=%d on %s after %d attempts: %s",
                       setting_id, value, self.ip, MAX_RETRIES, last_err)
        raise ConnectionError(
            f"Cannot set {setting_id}={value} on {self.ip}"
        ) from last_err

    async def _set_setting_once(self, setting_id: int, value: int) -> dict[int, int]:
        """Single attempt to set a setting. Returns device-confirmed values."""
        confirmed: dict[int, int] = {}
        async with websockets.connect(
            self.url,
            origin=WS_ORIGIN,
            ping_interval=None,
            close_timeout=5,
            open_timeout=5,
        ) as ws:
            cmd = self._build_set_setting(setting_id, value)
            await ws.send(cmd)
            await asyncio.sleep(0.5)

            # Read and parse device responses instead of discarding them
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1)
                    if isinstance(msg, bytes):
                        confirmed.update(self._parse_response(msg))
            except asyncio.TimeoutError:
                pass

        return confirmed

    async def async_set_settings(self, settings: dict[int, int]) -> dict[int, int]:
        """Set multiple settings on the topper. Returns confirmed values.

        Raises ConnectionError on failure after retries.
        """
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self._set_settings_once(settings)
            except Exception as err:
                last_err = err
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    _LOGGER.debug(
                        "Retry set_settings on %s (%d/%d) in %.1fs: %s",
                        self.ip, attempt + 1, MAX_RETRIES, delay, err,
                    )
                    await asyncio.sleep(delay)

        _LOGGER.error("Failed to set settings on %s after %d attempts: %s",
                       self.ip, MAX_RETRIES, last_err)
        raise ConnectionError(f"Cannot set settings on {self.ip}") from last_err

    async def _set_settings_once(self, settings: dict[int, int]) -> dict[int, int]:
        """Single attempt to set multiple settings. Returns confirmed values."""
        confirmed: dict[int, int] = {}
        async with websockets.connect(
            self.url,
            origin=WS_ORIGIN,
            ping_interval=None,
            close_timeout=5,
            open_timeout=5,
        ) as ws:
            for sid, val in settings.items():
                cmd = self._build_set_setting(sid, val)
                await ws.send(cmd)
                await asyncio.sleep(0.3)

            await asyncio.sleep(0.5)
            # Parse device responses
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1)
                    if isinstance(msg, bytes):
                        confirmed.update(self._parse_response(msg))
            except asyncio.TimeoutError:
                pass

        return confirmed

    def format_settings(self, raw: dict[int, int]) -> dict[str, Any]:
        """Format raw settings into a friendly dict."""
        result: dict[str, Any] = {}
        for sid, val in raw.items():
            if sid in TEMP_SETTINGS:
                result[sid] = {
                    "raw": val,
                    "celsius": raw_to_celsius(val),
                    "fahrenheit": raw_to_fahrenheit(val),
                }
            else:
                result[sid] = val
        return result
