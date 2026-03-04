"""Config flow for Perfectly Snug integration."""

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .client import TopperClient
from .const import CONF_LEFT_IP, CONF_RIGHT_IP, CONF_SINGLE_ZONE, DOMAIN

_LOGGER = logging.getLogger(__name__)


class PerfectlySnugConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Perfectly Snug Smart Topper."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — enter topper IP addresses."""
        errors: dict[str, str] = {}

        if user_input is not None:
            left_ip = user_input.get(CONF_LEFT_IP, "").strip()
            right_ip = user_input.get(CONF_RIGHT_IP, "").strip()
            single = user_input.get(CONF_SINGLE_ZONE, False)

            if not left_ip:
                errors[CONF_LEFT_IP] = "ip_required"
            else:
                client = TopperClient(left_ip)
                if not await client.async_test_connection():
                    errors[CONF_LEFT_IP] = "cannot_connect"

            if not single and right_ip:
                client = TopperClient(right_ip)
                if not await client.async_test_connection():
                    errors[CONF_RIGHT_IP] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(f"perfectly_snug_{left_ip}")
                self._abort_if_unique_id_configured()

                data = {CONF_LEFT_IP: left_ip, CONF_SINGLE_ZONE: single}
                if not single and right_ip:
                    data[CONF_RIGHT_IP] = right_ip

                return self.async_create_entry(
                    title="Perfectly Snug Smart Topper",
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LEFT_IP): str,
                    vol.Optional(CONF_RIGHT_IP): str,
                    vol.Optional(CONF_SINGLE_ZONE, default=False): bool,
                }
            ),
            errors=errors,
        )
