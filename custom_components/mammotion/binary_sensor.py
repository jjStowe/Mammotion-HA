"""Mammotion binary sensor entities."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymammotion.data.model.device import MowingDevice
from pymammotion.transport.base import TransportType
from pymammotion.utility.constant.device_constant import PosType, device_mode

from . import MammotionConfigEntry
from .const import LOGGER
from .coordinator import MammotionBaseUpdateCoordinator
from .entity import MammotionBaseEntity


@dataclass(frozen=True, kw_only=True)
class MammotionBinarySensorEntityDescription(
    BinarySensorEntityDescription,
):
    """Describes Mammotion binary sensor entity."""

    is_on_fn: Callable[["MammotionBinarySensorEntity", MowingDevice], bool | None]
    extra_attrs_fn: (
        Callable[["MammotionBinarySensorEntity", MowingDevice], dict[str, Any]] | None
    ) = None


DEPARTING_DOCK_ACCESS_MODES = {
    "MODE_WORKING",
    "MODE_MANUAL_MOWING",
}
RETURNING_DOCK_ACCESS_MODES = {"MODE_RETURNING", "MODE_CHARGING_PAUSE"}
DOCKED_DOCK_ACCESS_MODES = {"MODE_READY", "MODE_CHARGING", "MODE_NOT_ACTIVE"}
DEPARTURE_GRACE_SECONDS = 90
DOCK_ACCESS_MIN_REQUEST_SECONDS = 90
DOCK_ACCESS_CLOSE_DEBOUNCE_SECONDS = 20


def _get_nested(value: Any, *path: str) -> Any:
    """Return a nested attribute or mapping value without raising."""
    current = value
    for part in path:
        if current is None:
            return None
        try:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part)
        except (AttributeError, TypeError):
            return None
    return current


def _device_mode_name(sys_status: Any) -> str | None:
    if sys_status is None:
        return None
    try:
        return device_mode(sys_status)
    except (TypeError, ValueError):
        return str(sys_status)


def _position_type_name(position_type: Any) -> str | None:
    if position_type is None:
        return None
    try:
        return PosType(position_type).name
    except (TypeError, ValueError):
        return str(position_type)


def _raw_dock_access_values(mower_data: MowingDevice) -> dict[str, Any]:
    sys_status = _get_nested(mower_data, "report_data", "dev", "sys_status")
    position_type = _get_nested(mower_data, "location", "position_type")
    return {
        "sys_status": sys_status,
        "sys_status_name": _device_mode_name(sys_status),
        "charge_state": _get_nested(mower_data, "report_data", "dev", "charge_state"),
        "position_type": position_type,
        "position_type_name": _position_type_name(position_type),
        "work_zone": _get_nested(mower_data, "mowing_state", "zone_hash"),
    }


def _is_docked_or_charging(values: dict[str, Any]) -> bool:
    return (
        values["charge_state"] in (1, 2)
        or values["position_type_name"] == "CHARGE_ON"
    )


def _dock_access_signature(values: dict[str, Any]) -> tuple[Any, ...]:
    return (
        values["sys_status"],
        values["charge_state"],
        values["position_type"],
        values["work_zone"],
    )


def _dock_access_phase(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> str:
    """Return the current dock-access phase for attributes and logs."""
    sys_status_name = values["sys_status_name"]
    position_type_name = values["position_type_name"]
    docked_or_charging = _is_docked_or_charging(values)

    if sys_status_name in DEPARTING_DOCK_ACCESS_MODES:
        signature = _dock_access_signature(values)
        if entity._dock_access_departure_grace_active(signature):
            return "departing_dock_grace"
        if position_type_name == "CHARGE_ON":
            return "departing_dock"
        if position_type_name is None:
            if docked_or_charging or entity._was_docked_or_charging:
                return "departing_dock"
        if (
            entity._was_docked_or_charging
            and not entity._dock_access_departure_grace_used
        ):
            return "departing_dock_grace"
        return "away_from_dock"

    if docked_or_charging:
        return "docked"

    if sys_status_name in RETURNING_DOCK_ACCESS_MODES:
        return "returning_to_dock"

    if position_type_name not in (None, "CHARGE_ON"):
        return "away_from_dock"

    return "unknown"


def _dock_access_requested(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> bool | None:
    return entity._apply_dock_access_hysteresis(
        _dock_access_requested_raw(entity, mower_data)
    )


def _dock_access_requested_raw(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> bool:
    values = _raw_dock_access_values(mower_data)
    sys_status_name = values["sys_status_name"]
    docked_or_charging = _is_docked_or_charging(values)
    signature = _dock_access_signature(values)

    if docked_or_charging:
        entity._was_docked_or_charging = True
        entity._dock_access_departure_grace_used = False

    if sys_status_name in DEPARTING_DOCK_ACCESS_MODES:
        phase = _dock_access_phase(entity, values)
        if phase in ("departing_dock", "departing_dock_grace"):
            entity._start_dock_access_departure_grace(signature)
            return True
        entity._clear_dock_access_departure_grace()
        return False

    if docked_or_charging:
        entity._clear_dock_access_departure_grace()
        return False

    if sys_status_name in RETURNING_DOCK_ACCESS_MODES:
        entity._clear_dock_access_departure_grace()
        return True

    if sys_status_name in DOCKED_DOCK_ACCESS_MODES:
        entity._clear_dock_access_departure_grace()
        return False

    entity._clear_dock_access_departure_grace()
    return False


def _source_hint(coordinator: MammotionBaseUpdateCoordinator) -> str:
    handle = coordinator.manager.mower(coordinator.device_name)
    if handle is not None and handle.has_transport(TransportType.BLE):
        ble = handle.get_transport(TransportType.BLE)
        if ble is not None and ble.is_usable:
            return "ble"
    if coordinator.mqtt_transport_connected:
        return "cloud"
    if coordinator.mqtt_device_online:
        return "cloud_reported_online"
    return "unknown"


def _last_report_age_seconds(coordinator: MammotionBaseUpdateCoordinator) -> int | None:
    handle = coordinator.manager.mower(coordinator.device_name)
    last_report_at = getattr(handle, "last_report_at", None) if handle else None
    if last_report_at is None:
        return None

    if isinstance(last_report_at, datetime):
        age = datetime.now(last_report_at.tzinfo) - last_report_at
        return max(0, int(age.total_seconds()))

    if isinstance(last_report_at, (int, float)):
        return max(0, int(datetime.now().timestamp() - last_report_at))

    return None


def _dock_access_attributes(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> dict[str, Any]:
    values = _raw_dock_access_values(mower_data)
    values["access_phase"] = _dock_access_phase(entity, values)
    values["source_hint"] = _source_hint(entity.coordinator)
    values["last_report_age_seconds"] = _last_report_age_seconds(entity.coordinator)
    return values


BINARY_SENSORS: tuple[MammotionBinarySensorEntityDescription, ...] = (
    MammotionBinarySensorEntityDescription(
        key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        is_on_fn=lambda entity, mower_data: mower_data.report_data.dev.charge_state
        in (1, 2),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MammotionBinarySensorEntityDescription(
        key="dock_access_requested",
        is_on_fn=_dock_access_requested,
        extra_attrs_fn=_dock_access_attributes,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MammotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Mammotion sensor entity."""
    mammotion_devices = entry.runtime_data.mowers

    for mower in mammotion_devices:
        async_add_entities(
            MammotionBinarySensorEntity(mower.reporting_coordinator, entity_description)
            for entity_description in BINARY_SENSORS
        )


class MammotionBinarySensorEntity(MammotionBaseEntity, BinarySensorEntity):
    """Mammotion sensor entity."""

    entity_description: MammotionBinarySensorEntityDescription
    _was_docked_or_charging: bool
    _dock_access_departure_grace_signature: tuple[Any, ...] | None
    _dock_access_departure_grace_until: float | None
    _dock_access_departure_grace_used: bool
    _dock_access_logged_initial_state: bool
    _dock_access_last_logged_state: bool | None
    _dock_access_requested_state: bool | None
    _dock_access_state_changed_at: float | None
    _dock_access_close_pending_since: float | None

    def __init__(
        self,
        coordinator: MammotionBaseUpdateCoordinator,
        entity_description: MammotionBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor entity."""
        super().__init__(coordinator, entity_description.key)
        self.entity_description = entity_description
        self._attr_translation_key = (
            entity_description.translation_key or entity_description.key
        )
        self._was_docked_or_charging = False
        self._dock_access_departure_grace_signature = None
        self._dock_access_departure_grace_until = None
        self._dock_access_departure_grace_used = False
        self._dock_access_logged_initial_state = False
        self._dock_access_last_logged_state = None
        self._dock_access_requested_state = None
        self._dock_access_state_changed_at = None
        self._dock_access_close_pending_since = None

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        return self.entity_description.is_on_fn(self, self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.entity_description.extra_attrs_fn is None:
            return None
        return self.entity_description.extra_attrs_fn(self, self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Log dock access transitions before writing updated HA state."""
        if self.entity_description.key == "dock_access_requested":
            self._log_dock_access_transition()
        super()._handle_coordinator_update()

    def _log_dock_access_transition(self) -> None:
        """Write an audit line when dock access state changes."""
        if self.coordinator.data is None:
            return

        is_on = self.is_on
        if (
            self._dock_access_logged_initial_state
            and is_on == self._dock_access_last_logged_state
        ):
            return

        self._dock_access_logged_initial_state = True
        self._dock_access_last_logged_state = is_on
        attrs = _dock_access_attributes(self, self.coordinator.data)
        LOGGER.info(
            "Dock access requested for %s changed to %s "
            "(sys_status=%s sys_status_name=%s charge_state=%s "
            "position_type=%s position_type_name=%s work_zone=%s "
            "access_phase=%s last_report_age_seconds=%s source_hint=%s)",
            self.coordinator.device_name,
            is_on,
            attrs["sys_status"],
            attrs["sys_status_name"],
            attrs["charge_state"],
            attrs["position_type"],
            attrs["position_type_name"],
            attrs["work_zone"],
            attrs["access_phase"],
            attrs["last_report_age_seconds"],
            attrs["source_hint"],
        )

    def _dock_access_departure_grace_active(
        self,
        signature: tuple[Any, ...],
    ) -> bool:
        """Return whether this report is inside the departure grace window."""
        return (
            self._dock_access_departure_grace_signature == signature
            and self._dock_access_departure_grace_until is not None
            and time.monotonic() < self._dock_access_departure_grace_until
        )

    def _start_dock_access_departure_grace(
        self,
        signature: tuple[Any, ...],
    ) -> None:
        """Hold the departure open signal briefly for first post-dock reports."""
        if self._dock_access_departure_grace_active(signature):
            return

        self._clear_dock_access_departure_grace()
        self._dock_access_departure_grace_signature = signature
        self._dock_access_departure_grace_until = (
            time.monotonic() + DEPARTURE_GRACE_SECONDS
        )
        self._dock_access_departure_grace_used = True

    def _clear_dock_access_departure_grace(self) -> None:
        """Clear any pending departure grace timer."""
        self._dock_access_departure_grace_signature = None
        self._dock_access_departure_grace_until = None

    def _apply_dock_access_hysteresis(self, requested: bool) -> bool:
        """Hold request-on long enough to ignore noisy transition frames."""
        now = time.monotonic()
        if self._dock_access_requested_state is None:
            self._dock_access_requested_state = requested
            self._dock_access_state_changed_at = now
            self._dock_access_close_pending_since = None
            return requested

        if requested == self._dock_access_requested_state:
            if requested:
                self._dock_access_close_pending_since = None
            return self._dock_access_requested_state

        if self._dock_access_requested_state and not requested:
            if self._dock_access_close_pending_since is None:
                self._dock_access_close_pending_since = now

            changed_at = self._dock_access_state_changed_at
            if (
                changed_at is not None
                and now - changed_at < DOCK_ACCESS_MIN_REQUEST_SECONDS
            ):
                return True

            if (
                now - self._dock_access_close_pending_since
                < DOCK_ACCESS_CLOSE_DEBOUNCE_SECONDS
            ):
                return True

        if requested:
            self._dock_access_close_pending_since = None

        self._dock_access_requested_state = requested
        self._dock_access_state_changed_at = now
        return requested
