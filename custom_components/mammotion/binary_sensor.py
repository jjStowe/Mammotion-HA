"""Mammotion binary sensor entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from pymammotion.data.model.device import MowingDevice
from pymammotion.transport.base import TransportType
from pymammotion.utility.constant.device_constant import PosType, device_mode

from . import MammotionConfigEntry
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


ACTIVE_GARAGE_ACCESS_MODES = {
    "MODE_RETURNING",
    "MODE_WORKING",
    "MODE_MANUAL_MOWING",
    "MODE_CHARGING_PAUSE",
}
DOCKED_GARAGE_ACCESS_MODES = {"MODE_READY", "MODE_CHARGING", "MODE_NOT_ACTIVE"}


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


def _raw_garage_values(mower_data: MowingDevice) -> dict[str, Any]:
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


def _garage_access_needed(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> bool | None:
    values = _raw_garage_values(mower_data)
    sys_status_name = values["sys_status_name"]
    position_type_name = values["position_type_name"]
    docked_or_charging = _is_docked_or_charging(values)

    if docked_or_charging:
        entity._was_docked_or_charging = True

    if sys_status_name in ACTIVE_GARAGE_ACCESS_MODES:
        return True

    if docked_or_charging and sys_status_name in DOCKED_GARAGE_ACCESS_MODES:
        return False

    if entity._was_docked_or_charging and position_type_name not in (
        None,
        "CHARGE_ON",
    ):
        return True

    return None


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


def _garage_access_attributes(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> dict[str, Any]:
    values = _raw_garage_values(mower_data)
    values["source_hint"] = _source_hint(entity.coordinator)
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
        key="garage_access_needed",
        device_class=BinarySensorDeviceClass.GARAGE_DOOR,
        is_on_fn=_garage_access_needed,
        extra_attrs_fn=_garage_access_attributes,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
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
