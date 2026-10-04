"""Number platform for Tesla v1r integration — Tesla Energy Site controls."""

import logging
import time
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_TESLA_ENERGY_SITE_ID,
    DOMAIN,
    SENSOR_FAMILY_BATTERY,
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
    TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
    family_device_info,
)

_LOGGER = logging.getLogger(__name__)


def _fresh_powerwall_local_snapshot(
    hass: HomeAssistant, entry: ConfigEntry
) -> Any | None:
    """Return fresh local Powerwall data when paired, otherwise None."""
    if not entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        return None
    coordinator = (
        hass.data.get(DOMAIN, {})
        .get(entry.entry_id, {})
        .get("powerwall_local", {})
        .get("coordinator")
    )
    data = getattr(coordinator, "data", None)
    last_success_monotonic = getattr(coordinator, "last_success_monotonic", None)
    if data is None or last_success_monotonic is None:
        return None
    if time.monotonic() - last_success_monotonic > TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS:
        return None
    return data


def _positive_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _entry_value(entry: ConfigEntry, key: str) -> Any:
    if key in entry.options and entry.options.get(key) is not None:
        return entry.options.get(key)
    return entry.data.get(key)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PowerSync number entities."""
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID,
        entry.data.get(CONF_TESLA_ENERGY_SITE_ID, ""),
    )

    if tesla_site_id:
        # Backup reserve is universally supported for any Tesla energy site.
        async_add_entities([BackupReserveNumber(hass, entry)])


class _TeslaSiteNumberBase(NumberEntity):
    _attr_has_entity_name = True
    _attr_should_poll = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        name: str,
        icon: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name
        self._attr_icon = icon
        # No EntityCategory — these are user-facing controls (Backup Reserve etc),
        # belong in the device card's main Controls section, not Configuration.

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _tesla_coord(self):
        return (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_coordinator")
        )

    async def async_update(self) -> None:
        """Refresh Tesla site_info often enough for controls changed elsewhere."""
        coord = self._tesla_coord()
        if coord is None:
            return
        try:
            await coord.async_get_site_info(
                max_age=TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
            )
        except Exception:
            _LOGGER.debug(
                "Could not refresh Tesla site_info for number entity",
                exc_info=True,
            )


class BackupReserveNumber(_TeslaSiteNumberBase):
    """Backup reserve % for Tesla Powerwall."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_backup_reserve",
            name="Backup Reserve",
            icon="mdi:battery-lock",
        )

    @property
    def native_value(self) -> float | None:
        # Local raw reserve has no authoritative user-scale conversion.
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if site_info and "backup_reserve_percent" in site_info:
            reserve = site_info["backup_reserve_percent"]
            if reserve is None:
                return None
            return float(reserve)
        stored = self._entry.options.get("_user_backup_reserve")
        return float(stored) if stored is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_backup_reserve",
            {"percent": int(value), "source": "user"},
            blocking=False,
        )
