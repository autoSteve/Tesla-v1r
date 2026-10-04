"""Switch platform for Tesla v1r integration."""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATTR_LAST_SYNC,
    ATTR_SYNC_STATUS,
    BATTERY_SYSTEM_TESLA,
    CONF_AUTO_SYNC_ENABLED,
    CONF_BATTERY_SYSTEM,
    CONF_FORCE_CHARGE_DURATION,
    CONF_FORCE_DISCHARGE_DURATION,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_TESLA_ENERGY_SITE_ID,
    DEFAULT_DISCHARGE_DURATION,
    DOMAIN,
    POWERWALL_LOCAL_POLL_INTERVAL,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_LP_OPTIMIZER,
    SWITCH_TYPE_FORCE_CHARGE,
    SWITCH_TYPE_FORCE_DISCHARGE,
    TESLA_CAPABILITY_WAIT_SECONDS,
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
    TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS,
    family_device_info,
)
from .tesla_grid_control import tesla_grid_charging_enabled_from_site_info

# Providers that use TOU schedule syncing (Amber, Octopus, Flow Power)
# GloBird and AEMO VPP use spike detection only — no TOU sync
PROVIDERS_WITH_TOU_SYNC = {"amber", "octopus", "flow_power"}

_LOGGER = logging.getLogger(__name__)


def _grid_status_is_off_grid(value: Any) -> bool | None:
    """Classify only terminal grid states; preserve transitions as unknown."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"active", "systemgridconnected"}:
        return False
    if normalized in {
        "inactive",
        "islanded",
        "off-grid",
        "systemislandedactive",
    }:
        return True
    return None


def _fresh_powerwall_local_snapshot(
    hass: HomeAssistant, entry: ConfigEntry
) -> Any | None:
    """Return a fresh paired local Powerwall snapshot, if available."""
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


def _coerce_duration(value: Any, default: int = DEFAULT_DISCHARGE_DURATION) -> int:
    """Return a valid integer duration for manual force controls."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _selected_duration(entry: ConfigEntry, key: str) -> int:
    """Read the duration selected by the matching select entity."""
    return _coerce_duration(
        entry.options.get(key, entry.data.get(key, DEFAULT_DISCHARGE_DURATION))
    )


def _state_float(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _selected_force_power_w(hass: HomeAssistant) -> int:
    """Read the force-power selector and convert it to service power_w."""
    watts = _state_float(hass, "number.tesla_v1r_force_power_w")
    if watts is not None and watts > 0:
        return round(watts)

    kw = _state_float(hass, "number.tesla_v1r_force_power_kw")
    if kw is not None and kw > 0:
        return round(kw * 1000)

    return 0


def _parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _datetime_now_for(expires_at: datetime) -> datetime:
    return datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()


async def _reoptimize_if_enabled(coordinator: Any, changed: bool) -> None:
    """Refresh the LP plan after an optimizer setting switch changes."""
    if (
        changed
        and bool(getattr(coordinator, "enabled", False))
        and hasattr(coordinator, "force_reoptimize")
    ):
        await coordinator.force_reoptimize()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tesla v1r switch entities."""
    # Detect Tesla by checking if tesla_energy_site_id is configured
    tesla_site_id = entry.options.get(
        CONF_TESLA_ENERGY_SITE_ID, entry.data.get(CONF_TESLA_ENERGY_SITE_ID, "")
    )
    is_tesla = bool(tesla_site_id)

    battery_system = entry.options.get(
        CONF_BATTERY_SYSTEM,
        entry.data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA),
    )

    _LOGGER.info(
        "🔋 Switch setup: is_tesla=%s, battery_system=%s",
        is_tesla,
        battery_system,
    )

    entities = []

    # Manual force controls are service wrappers and are available for every
    # battery system supported by tesla_v1r.force_charge/force_discharge.
    entities.append(
        [
            ForceDischargeSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_FORCE_DISCHARGE,
                    name="Force Discharge",
                    icon="mdi:battery-arrow-up",
                ),
            ),
            ForceChargeSwitch(
                hass=hass,
                entry=entry,
                description=SwitchEntityDescription(
                    key=SWITCH_TYPE_FORCE_CHARGE,
                    name="Force Charge",
                    icon="mdi:battery-arrow-down",
                ),
            ),
        ]
    )

    # Add Tesla-specific switches only if Tesla is selected as battery system
    if is_tesla:
        _LOGGER.info("Tesla battery system detected - adding Tesla-specific switches")
        entities.extend(
            [
                GridChargingSwitch(hass=hass, entry=entry),
            ]
        )

    # Off-grid switch — available when Powerwall is paired for local control
    if is_tesla and entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        entities.extend(
            [
                OffGridSwitch(hass=hass, entry=entry),
                OnGridSwitch(hass=hass, entry=entry),
            ]
        )

    async_add_entities(entities)

    # Capability-gated Tesla entities (storm watch, VPP program switches).
    # These cannot be added until the Tesla capability probe completes,
    # which runs ~after the first site_info fetch. We wait for that in a
    # background task and add them once.
    if is_tesla:

        async def _add_capability_gated_switches() -> None:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            waited = 0.0
            while (
                "tesla_capabilities" not in entry_data
                and waited < TESLA_CAPABILITY_WAIT_SECONDS
            ):
                await asyncio.sleep(2.0)
                waited += 2.0
                entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            caps = entry_data.get("tesla_capabilities", {})
            if not caps:
                _LOGGER.info(
                    "Tesla capability probe did not complete within %.0fs — "
                    "skipping capability-gated switch creation",
                    TESLA_CAPABILITY_WAIT_SECONDS,
                )
                return

            tesla_coord = entry_data.get("tesla_coordinator")
            to_add: list[SwitchEntity] = []
            if caps.get("storm_mode"):
                to_add.append(StormWatchSwitch(hass=hass, entry=entry))
            if caps.get("vpp_programs") and tesla_coord is not None:
                programs = getattr(tesla_coord, "_vpp_programs_cache", None) or []
                for program in programs:
                    to_add.append(
                        VppProgramSwitch(hass=hass, entry=entry, program=program)
                    )
            if to_add:
                _LOGGER.info(
                    "Adding %d capability-gated Tesla switches (storm_mode=%s, vpp=%d)",
                    len(to_add),
                    caps.get("storm_mode"),
                    len(getattr(tesla_coord, "_vpp_programs_cache", None) or [])
                    if tesla_coord
                    else 0,
                )
                async_add_entities(to_add)

        hass.async_create_task(
            _add_capability_gated_switches(),
            name=f"{DOMAIN}_capability_gated_switches",
        )


class AutoSyncSwitch(SwitchEntity):
    """Switch to enable/disable automatic TOU schedule syncing."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"tesla_v1r_{description.key}"

        # Initialize state from config
        self._attr_is_on = entry.options.get(
            CONF_AUTO_SYNC_ENABLED,
            entry.data.get(CONF_AUTO_SYNC_ENABLED, True),
        )

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_LP_OPTIMIZER)

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info(
                "Auto-sync switch activated (context: user_id=%s, parent_id=%s)",
                context.user_id,
                context.parent_id,
            )
        else:
            _LOGGER.info("Auto-sync switch activated (no context - likely UI action)")
        _LOGGER.info("Enabling automatic TOU schedule syncing")
        self._attr_is_on = True

        # Update config entry options
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_SYNC_ENABLED] = True
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()

        # Trigger an immediate sync
        await self.hass.services.async_call(
            DOMAIN,
            "sync_tou_schedule",
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        _LOGGER.info("Disabling automatic TOU schedule syncing")
        self._attr_is_on = False

        # Update config entry options
        new_options = {**self._entry.options}
        new_options[CONF_AUTO_SYNC_ENABLED] = False
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=new_options,
        )

        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        amber_coordinator = domain_data.get("amber_coordinator")

        attrs = {}

        if amber_coordinator and amber_coordinator.data:
            attrs[ATTR_LAST_SYNC] = amber_coordinator.data.get("last_update")
            attrs[ATTR_SYNC_STATUS] = "enabled" if self.is_on else "disabled"

        return attrs

class ForceDischargeSwitch(SwitchEntity):
    """Switch to manually force battery discharge mode."""

    _attr_has_entity_name = True
    # Primary user control — belongs in the device card's Controls section.

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"tesla_v1r_{description.key}"
        self._attr_is_on = False
        self._discharge_expires_at: datetime | None = None
        self._duration_minutes: int = DEFAULT_DISCHARGE_DURATION
        self._cancel_expiry_timer = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def is_on(self) -> bool:
        """Return True if force discharge is active."""
        return self._attr_is_on

    async def async_added_to_hass(self) -> None:
        """Keep switch state in sync with service/dashboard-triggered discharge."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_force_discharge_state",
                self._handle_force_discharge_update,
            )
        )

    @callback
    def _handle_force_discharge_update(self, state: dict[str, Any]) -> None:
        """Update switch state when force discharge is changed elsewhere."""
        active = bool(state.get("active")) if isinstance(state, dict) else False
        self._attr_is_on = active
        if isinstance(state, dict) and state.get("duration") is not None:
            self._duration_minutes = _coerce_duration(
                state.get("duration"), self._duration_minutes
            )
        self._discharge_expires_at = None
        if active and isinstance(state, dict):
            self._discharge_expires_at = _parse_expiry(state.get("expires_at"))
        if active and self._discharge_expires_at:
            self._schedule_expiry_check()
        elif self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force discharge mode."""
        # Log context to help debug if triggered by automation vs user
        context = kwargs.get("context")
        if context:
            _LOGGER.info(
                "Force discharge switch activated (context: user_id=%s, parent_id=%s)",
                context.user_id,
                context.parent_id,
            )
        else:
            _LOGGER.info(
                "Force discharge switch activated (no context - likely UI action)"
            )
        _LOGGER.info(
            "Activating force discharge mode for %d minutes", self._duration_minutes
        )

        # Get the duration from service call data if provided
        selected_duration = _selected_duration(
            self._entry,
            CONF_FORCE_DISCHARGE_DURATION,
        )
        duration = _coerce_duration(
            kwargs.get("duration", selected_duration),
            self._duration_minutes,
        )
        # This entity is a user-facing manual control. Mark the nested service
        # call explicitly so Monitoring Mode does not mistake it for an
        # optimizer/automation command when Home Assistant drops UI context.
        service_data = {"duration": duration, "source": "user"}
        power_w = _selected_force_power_w(self.hass)
        if power_w > 0:
            service_data["power_w"] = power_w

        # Call the force discharge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_discharge",
                service_data,
                blocking=True,
            )

        except Exception as err:
            _LOGGER.error("Failed to activate force discharge: %s", err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off force discharge mode (restore normal operation)."""
        _LOGGER.info("Deactivating force discharge mode, restoring normal operation")

        try:
            await self.hass.services.async_call(
                DOMAIN,
                "restore_normal",
                {"source": "user"},
                blocking=True,
            )

        except Exception as err:
            _LOGGER.error("Failed to restore normal operation: %s", err)
            raise

    def _schedule_expiry_check(self) -> None:
        """Schedule periodic check for discharge expiry."""
        # Cancel any existing timer
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()

        @callback
        def _check_expiry(now: datetime) -> None:
            """Check if discharge has expired."""
            if (
                self._discharge_expires_at
                and _datetime_now_for(self._discharge_expires_at)
                >= self._discharge_expires_at
            ):
                _LOGGER.info("Force discharge expired, restoring normal operation")
                # The service-side expiry callback owns the physical restore.
                # If its confirmation fails, keep the dispatcher-owned state
                # on until a confirmed restore changes it.
                self._cancel_expiry_timer = None
            elif self._attr_is_on:
                # Schedule next check
                self._schedule_expiry_check()

        # Check every 30 seconds
        self._cancel_expiry_timer = async_track_time_interval(
            self.hass, _check_expiry, timedelta(seconds=30)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "duration_minutes": self._duration_minutes,
        }

        if self._discharge_expires_at:
            attrs["expires_at"] = self._discharge_expires_at.isoformat()
            remaining = self._discharge_expires_at - _datetime_now_for(
                self._discharge_expires_at
            )
            if remaining.total_seconds() > 0:
                attrs["remaining_minutes"] = int(remaining.total_seconds() / 60)
            else:
                attrs["remaining_minutes"] = 0

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None


class ForceChargeSwitch(SwitchEntity):
    """Switch to manually force battery charge mode."""

    _attr_has_entity_name = True
    # Primary user control — belongs in the device card's Controls section.

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: SwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"tesla_v1r_{description.key}"
        self._attr_is_on = False
        self._charge_expires_at: datetime | None = None
        self._duration_minutes: int = DEFAULT_DISCHARGE_DURATION  # Reuse same default
        self._cancel_expiry_timer = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    @property
    def is_on(self) -> bool:
        """Return True if force charge is active."""
        return self._attr_is_on

    async def async_added_to_hass(self) -> None:
        """Keep switch state in sync with service/dashboard-triggered charge."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_force_charge_state",
                self._handle_force_charge_update,
            )
        )

    @callback
    def _handle_force_charge_update(self, state: dict[str, Any]) -> None:
        """Update switch state when force charge is changed elsewhere."""
        active = bool(state.get("active")) if isinstance(state, dict) else False
        self._attr_is_on = active
        if isinstance(state, dict) and state.get("duration") is not None:
            self._duration_minutes = _coerce_duration(
                state.get("duration"), self._duration_minutes
            )
        self._charge_expires_at = None
        if active and isinstance(state, dict):
            self._charge_expires_at = _parse_expiry(state.get("expires_at"))
        if active and self._charge_expires_at:
            self._schedule_expiry_check()
        elif self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on force charge mode."""
        _LOGGER.info(
            "Activating force charge mode for %d minutes", self._duration_minutes
        )

        # Get the duration from service call data if provided
        selected_duration = _selected_duration(
            self._entry,
            CONF_FORCE_CHARGE_DURATION,
        )
        duration = _coerce_duration(
            kwargs.get("duration", selected_duration),
            self._duration_minutes,
        )
        # This entity is a user-facing manual control. Mark the nested service
        # call explicitly so Monitoring Mode does not mistake it for an
        # optimizer/automation command when Home Assistant drops UI context.
        service_data = {"duration": duration, "source": "user"}
        power_w = _selected_force_power_w(self.hass)
        if power_w > 0:
            service_data["power_w"] = power_w

        # Call the force charge service
        try:
            await self.hass.services.async_call(
                DOMAIN,
                "force_charge",
                service_data,
                blocking=True,
            )

        except Exception as err:
            _LOGGER.error("Failed to activate force charge: %s", err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off force charge mode (restore normal operation)."""
        _LOGGER.info("Deactivating force charge mode, restoring normal operation")

        try:
            await self.hass.services.async_call(
                DOMAIN,
                "restore_normal",
                {"source": "user"},
                blocking=True,
            )

        except Exception as err:
            _LOGGER.error("Failed to restore normal operation: %s", err)
            raise

    def _schedule_expiry_check(self) -> None:
        """Schedule periodic check for charge expiry."""
        # Cancel any existing timer
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()

        @callback
        def _check_expiry(now: datetime) -> None:
            """Check if charge has expired."""
            if (
                self._charge_expires_at
                and _datetime_now_for(self._charge_expires_at)
                >= self._charge_expires_at
            ):
                _LOGGER.info("Force charge expired, restoring normal operation")
                # The service-side expiry callback owns the physical restore.
                # If its confirmation fails, keep the dispatcher-owned state
                # on until a confirmed restore changes it.
                self._cancel_expiry_timer = None
            elif self._attr_is_on:
                # Schedule next check
                self._schedule_expiry_check()

        # Check every 30 seconds
        self._cancel_expiry_timer = async_track_time_interval(
            self.hass, _check_expiry, timedelta(seconds=30)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "duration_minutes": self._duration_minutes,
        }

        if self._charge_expires_at:
            attrs["expires_at"] = self._charge_expires_at.isoformat()
            remaining = self._charge_expires_at - _datetime_now_for(
                self._charge_expires_at
            )
            if remaining.total_seconds() > 0:
                attrs["remaining_minutes"] = int(remaining.total_seconds() / 60)
            else:
                attrs["remaining_minutes"] = 0

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._cancel_expiry_timer:
            self._cancel_expiry_timer()
            self._cancel_expiry_timer = None


class _TeslaSiteSwitchBase(SwitchEntity):
    """Base for Tesla Energy Site switches that call coordinator methods."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, key: str, name: str, icon: str
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"tesla_v1r_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_is_on: bool | None = None

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
                "Could not refresh Tesla site_info for switch entity",
                exc_info=True,
            )


class GridChargingSwitch(_TeslaSiteSwitchBase):
    """Toggle whether the Powerwall may charge from grid (TOU arbitrage)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_grid_charging",
            name="Grid Charging",
            icon="mdi:transmission-tower-import",
        )

    @property
    def is_on(self) -> bool | None:
        local_snapshot = _fresh_powerwall_local_snapshot(self.hass, self._entry)
        local_enabled = getattr(local_snapshot, "grid_charging_enabled", None)
        if local_enabled is not None:
            return bool(local_enabled)
        coord = self._tesla_coord()
        site_info = getattr(coord, "_site_info_cache", None) if coord else None
        if not site_info:
            return self._attr_is_on
        observed = tesla_grid_charging_enabled_from_site_info(site_info)
        if observed is not None:
            return observed
        remembered = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("tesla_grid_charging_preferences", {})
            .get(str(self._entry.data.get(CONF_TESLA_ENERGY_SITE_ID, "")))
        )
        return remembered if isinstance(remembered, bool) else self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_grid_charging",
            {"enabled": True, "source": "user"},
            blocking=False,
        )
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_grid_charging",
            {"enabled": False, "source": "user"},
            blocking=False,
        )
        self._attr_is_on = False
        self.async_write_ha_state()


class StormWatchSwitch(_TeslaSiteSwitchBase):
    """Toggle Tesla Storm Watch (predictive pre-charging before severe weather)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="tesla_storm_watch",
            name="Storm Watch",
            icon="mdi:weather-lightning",
        )

    @property
    def is_on(self) -> bool | None:
        coord = self._tesla_coord()
        if coord is None:
            return None
        return getattr(coord, "_storm_mode_enabled", None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_storm_watch",
            {"enabled": True, "source": "user"},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_storm_watch",
            {"enabled": False, "source": "user"},
            blocking=False,
        )


class VppProgramSwitch(_TeslaSiteSwitchBase):
    """Enrollment toggle for a single Tesla VPP / grid-services program."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, program: dict) -> None:
        pid = (
            program.get("id")
            or program.get("program_id")
            or program.get("name")
            or "unknown"
        )
        pid_str = str(pid)
        safe_key = "".join(c if c.isalnum() else "_" for c in pid_str.lower())
        display_name = program.get("display_name") or program.get("name") or pid_str
        super().__init__(
            hass,
            entry,
            key=f"tesla_vpp_{safe_key}",
            name=f"VPP: {display_name}",
            icon="mdi:transmission-tower",
        )
        self._program_id = pid_str
        self._program = program

    def _current_program(self) -> dict | None:
        coord = self._tesla_coord()
        if coord is None:
            return self._program
        programs = getattr(coord, "_vpp_programs_cache", None) or []
        for p in programs:
            if (
                str(p.get("id") or p.get("program_id") or p.get("name"))
                == self._program_id
            ):
                return p
        return self._program

    @property
    def is_on(self) -> bool | None:
        p = self._current_program()
        if not p:
            return None
        val = p.get("enrolled")
        if val is None:
            val = p.get("is_enrolled")
        if val is None:
            val = p.get("user_enrolled")
        return bool(val) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self._current_program() or {}
        return {
            "program_id": self._program_id,
            "display_name": p.get("display_name") or p.get("name"),
            "description": p.get("description"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": True, "source": "user"},
            blocking=False,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": self._program_id, "enrolled": False, "source": "user"},
            blocking=False,
        )


class _PowerwallGridModeSwitch(SwitchEntity):
    """Mutually-exclusive Powerwall grid connection mode switch."""

    _attr_has_entity_name = True
    _PENDING_SECONDS = 120

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
        icon: str,
        mode_is_off_grid: bool,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"tesla_v1r_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._mode_is_off_grid = mode_is_off_grid

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    def _entry_data(self) -> dict[str, Any]:
        return self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self._entry.entry_id, {}
        )

    def _current_is_off_grid(self) -> bool | None:
        """Return actual islanding state from local data, falling back to cloud."""
        runtime = self._entry_data().get("powerwall_local") or {}
        coord = runtime.get("coordinator")
        if coord is not None:
            snap = coord.data
            if snap is not None:
                return _grid_status_is_off_grid(getattr(snap, "grid_status", None))

        # Fall back to cloud grid_status sensor
        state = self.hass.states.get("sensor.tesla_v1r_grid_status")
        return _grid_status_is_off_grid(getattr(state, "state", None))

    def _pending_state(self) -> tuple[bool | None, datetime | None]:
        pending = self._entry_data().get("powerwall_grid_mode_pending") or {}
        is_off_grid = pending.get("is_off_grid")
        expires_at = pending.get("expires_at")
        if isinstance(is_off_grid, bool) and isinstance(expires_at, datetime):
            return is_off_grid, expires_at
        return None, None

    def _set_pending_state(self, off_grid: bool) -> None:
        self._entry_data()["powerwall_grid_mode_pending"] = {
            "is_off_grid": off_grid,
            "expires_at": datetime.now() + timedelta(seconds=self._PENDING_SECONDS),
        }

    def _clear_pending_state(self) -> None:
        self._entry_data().pop("powerwall_grid_mode_pending", None)

    def _effective_is_off_grid(self) -> bool | None:
        """Return actual mode, with a short optimistic window after commands."""
        actual = self._current_is_off_grid()
        pending_is_off_grid, pending_expires_at = self._pending_state()
        if pending_is_off_grid is None:
            return actual
        if actual is None:
            return None

        if actual == pending_is_off_grid:
            self._clear_pending_state()
            return actual

        if pending_expires_at and datetime.now() < pending_expires_at:
            return pending_is_off_grid

        self._clear_pending_state()
        return actual

    @property
    def is_on(self) -> bool | None:
        """Return True when this mode is active."""
        is_off_grid = self._effective_is_off_grid()
        if is_off_grid is None:
            return None
        return is_off_grid == self._mode_is_off_grid

    async def _set_grid_mode(self, off_grid: bool) -> None:
        service = "powerwall_go_off_grid" if off_grid else "powerwall_reconnect_grid"
        mode = "off-grid" if off_grid else "on-grid"
        _LOGGER.info("Powerwall grid mode switch: setting %s", mode)
        await self.hass.services.async_call(
            DOMAIN,
            service,
            {},
            blocking=True,
        )
        self._set_pending_state(off_grid)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate this grid mode."""
        await self._set_grid_mode(self._mode_is_off_grid)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Activate the opposite grid mode."""
        await self._set_grid_mode(not self._mode_is_off_grid)

    async def async_added_to_hass(self) -> None:
        """Keep the mutually-exclusive mode switches fresh during transitions."""
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_state_tick,
                timedelta(seconds=POWERWALL_LOCAL_POLL_INTERVAL),
            )
        )

    @callback
    def _handle_state_tick(self, now: datetime) -> None:
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return command-transition details."""
        actual = self._current_is_off_grid()
        pending_is_off_grid, pending_expires_at = self._pending_state()
        attrs: dict[str, Any] = {}
        if actual is not None:
            attrs["actual_grid_mode"] = "off_grid" if actual else "on_grid"
        if pending_is_off_grid is not None:
            attrs["pending_grid_mode"] = (
                "off_grid" if pending_is_off_grid else "on_grid"
            )
            if pending_expires_at is not None:
                attrs["pending_expires_at"] = pending_expires_at.isoformat()
        return attrs


class OffGridSwitch(_PowerwallGridModeSwitch):
    """Switch that is on while the Powerwall is intentionally islanded."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="off_grid",
            name="Off-Grid",
            icon="mdi:transmission-tower-off",
            mode_is_off_grid=True,
        )


class OnGridSwitch(_PowerwallGridModeSwitch):
    """Switch that is on while the Powerwall is connected to grid."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            entry,
            key="on_grid",
            name="On-Grid",
            icon="mdi:transmission-tower",
            mode_is_off_grid=False,
        )
