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


def raw_to_celsius(raw: int) -> float:
    """Convert raw sensor value to Celsius."""
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
        """Fetch settings from the topper."""
        if setting_ids is None:
            setting_ids = POLL_SETTINGS

        readings: dict[int, int] = {}
        try:
            async with websockets.connect(
                self.url,
                origin=WS_ORIGIN,
                ping_interval=None,
                close_timeout=5,
                open_timeout=5,
            ) as ws:
                # Send in batches of 8
                for i in range(0, len(setting_ids), 8):
                    batch = setting_ids[i : i + 8]
                    await ws.send(self._build_get_settings(batch))
                    await asyncio.sleep(0.2)

                # Collect responses
                deadline = asyncio.get_event_loop().time() + 10
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=3)
                        if isinstance(msg, bytes):
                            readings.update(self._parse_response(msg))
                        if len(readings) >= len(setting_ids):
                            break
                    except asyncio.TimeoutError:
                        break
        except Exception as err:
            _LOGGER.error("Failed to get settings from %s: %s", self.ip, err)
            raise ConnectionError(f"Cannot reach topper at {self.ip}") from err

        return readings

    async def async_set_setting(self, setting_id: int, value: int) -> bool:
        """Set a single setting on the topper."""
        try:
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

                # Drain any responses
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=1)
                except asyncio.TimeoutError:
                    pass

            return True
        except Exception as err:
            _LOGGER.error(
                "Failed to set setting %d=%d on %s: %s",
                setting_id,
                value,
                self.ip,
                err,
            )
            return False

    async def async_set_settings(self, settings: dict[int, int]) -> bool:
        """Set multiple settings on the topper."""
        try:
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
                # Drain responses
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=1)
                except asyncio.TimeoutError:
                    pass

            return True
        except Exception as err:
            _LOGGER.error("Failed to set settings on %s: %s", self.ip, err)
            return False

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
