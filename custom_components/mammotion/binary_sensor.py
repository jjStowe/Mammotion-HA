"""Mammotion binary sensor entities."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
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
RETURNING_MODE = "MODE_RETURNING"
CHARGING_PAUSE_MODE = "MODE_CHARGING_PAUSE"
PAUSE_MODE = "MODE_PAUSE"
DOCKED_DOCK_ACCESS_MODES = {"MODE_READY", "MODE_CHARGING", "MODE_NOT_ACTIVE"}
ARRIVAL_CONFIRMATION_MODES = DOCKED_DOCK_ACCESS_MODES | {CHARGING_PAUSE_MODE}
DEPARTURE_GRACE_SECONDS = 90
DOCK_ACCESS_MIN_REQUEST_SECONDS = 60
DOCK_ACCESS_CLOSE_DEBOUNCE_SECONDS = 15
ARRIVAL_DOCKED_STABLE_SECONDS = 20
IMPLICIT_ARRIVAL_DOCKED_STABLE_SECONDS = 60


class DockAccessJourney(StrEnum):
    """Track the mower's journey relative to the charging dock."""

    ARRIVED = "arrived"
    DEPARTING = "departing"
    AWAY = "away"
    RETURNING = "returning"


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


def _is_away_from_dock(values: dict[str, Any]) -> bool:
    """Return whether telemetry no longer contains dock evidence."""
    return not _is_docked_or_charging(values)


def _initial_dock_access_journey(values: dict[str, Any]) -> DockAccessJourney:
    """Classify an initial snapshot without opening for an active mid-yard job."""
    sys_status_name = values["sys_status_name"]
    docked_or_charging = _is_docked_or_charging(values)

    if sys_status_name in DEPARTING_DOCK_ACCESS_MODES:
        return (
            DockAccessJourney.DEPARTING
            if docked_or_charging
            else DockAccessJourney.AWAY
        )
    if sys_status_name == RETURNING_MODE:
        return (
            DockAccessJourney.ARRIVED
            if docked_or_charging
            else DockAccessJourney.RETURNING
        )
    if sys_status_name in DOCKED_DOCK_ACCESS_MODES or docked_or_charging:
        return DockAccessJourney.ARRIVED
    return DockAccessJourney.AWAY


def _set_dock_access_journey(
    entity: "MammotionBinarySensorEntity", journey: DockAccessJourney
) -> None:
    """Enter a dock journey state and reset state-specific timers."""
    if entity._dock_access_journey == journey:
        return

    entity._cancel_dock_access_journey_reevaluation()
    entity._dock_access_journey = journey
    entity._dock_access_journey_started_at = time.monotonic()
    entity._dock_access_arrival_candidate_since = None
    if journey == DockAccessJourney.DEPARTING:
        entity._schedule_dock_access_journey_reevaluation(DEPARTURE_GRACE_SECONDS)


def _clear_dock_access_arrival_candidate(
    entity: "MammotionBinarySensorEntity",
) -> None:
    """Discard arrival evidence and restore any pending departure timer."""
    if entity._dock_access_arrival_candidate_since is None:
        return

    entity._dock_access_arrival_candidate_since = None
    entity._cancel_dock_access_journey_reevaluation()
    if (
        entity._dock_access_journey == DockAccessJourney.DEPARTING
        and entity._dock_access_journey_started_at is not None
    ):
        remaining = DEPARTURE_GRACE_SECONDS - (
            time.monotonic() - entity._dock_access_journey_started_at
        )
        if remaining > 0:
            entity._schedule_dock_access_journey_reevaluation(remaining)


def _dock_access_arrival_is_stable(
    entity: "MammotionBinarySensorEntity",
    values: dict[str, Any],
    stable_seconds: float = ARRIVAL_DOCKED_STABLE_SECONDS,
) -> bool:
    """Return whether credible arrival evidence has remained stable long enough."""
    if not _is_docked_or_charging(values):
        _clear_dock_access_arrival_candidate(entity)
        return False

    now = time.monotonic()
    if entity._dock_access_arrival_candidate_since is None:
        entity._cancel_dock_access_journey_reevaluation()
        entity._dock_access_arrival_candidate_since = now
        entity._schedule_dock_access_journey_reevaluation(stable_seconds)
        return False

    elapsed = now - entity._dock_access_arrival_candidate_since
    if elapsed < stable_seconds:
        entity._schedule_dock_access_journey_reevaluation(
            stable_seconds - elapsed
        )
        return False
    return True


def _dock_access_phase(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> str:
    """Return the explicit dock journey state for attributes and logs."""
    journey = entity._dock_access_journey or _initial_dock_access_journey(values)
    return journey.value


def _start_dock_access_return(
    entity: "MammotionBinarySensorEntity",
) -> None:
    """Start a return after an explicit returning-mode report."""
    _set_dock_access_journey(entity, DockAccessJourney.RETURNING)


def _transition_dock_access_from_arrived(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> None:
    """Apply transitions from the arrived state."""
    sys_status_name = values["sys_status_name"]

    _clear_dock_access_arrival_candidate(entity)
    if sys_status_name in DEPARTING_DOCK_ACCESS_MODES:
        _set_dock_access_journey(entity, DockAccessJourney.DEPARTING)
    elif sys_status_name == RETURNING_MODE:
        _set_dock_access_journey(entity, DockAccessJourney.RETURNING)


def _transition_dock_access_from_departing(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> None:
    """Apply transitions from the departing state."""
    sys_status_name = values["sys_status_name"]
    docked_or_charging = _is_docked_or_charging(values)

    if sys_status_name == RETURNING_MODE:
        _start_dock_access_return(entity)
        return
    if sys_status_name in DOCKED_DOCK_ACCESS_MODES and docked_or_charging:
        if _dock_access_arrival_is_stable(entity, values):
            _set_dock_access_journey(entity, DockAccessJourney.ARRIVED)
        return

    _clear_dock_access_arrival_candidate(entity)
    journey_started_at = entity._dock_access_journey_started_at
    if journey_started_at is None:
        return

    elapsed = time.monotonic() - journey_started_at
    if elapsed >= DEPARTURE_GRACE_SECONDS and _is_away_from_dock(values):
        _set_dock_access_journey(entity, DockAccessJourney.AWAY)
    elif elapsed < DEPARTURE_GRACE_SECONDS:
        entity._schedule_dock_access_journey_reevaluation(
            DEPARTURE_GRACE_SECONDS - elapsed
        )


def _transition_dock_access_from_away(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> None:
    """Apply transitions from the away state."""
    sys_status_name = values["sys_status_name"]
    if sys_status_name == RETURNING_MODE:
        _start_dock_access_return(entity)
    elif sys_status_name in ARRIVAL_CONFIRMATION_MODES:
        if _dock_access_arrival_is_stable(
            entity,
            values,
            IMPLICIT_ARRIVAL_DOCKED_STABLE_SECONDS,
        ):
            _set_dock_access_journey(entity, DockAccessJourney.ARRIVED)
    else:
        _clear_dock_access_arrival_candidate(entity)


def _transition_dock_access_from_returning(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> None:
    """Apply transitions from the returning state."""
    sys_status_name = values["sys_status_name"]
    docked_or_charging = _is_docked_or_charging(values)

    if sys_status_name in DEPARTING_DOCK_ACCESS_MODES:
        _set_dock_access_journey(
            entity,
            DockAccessJourney.DEPARTING
            if docked_or_charging
            else DockAccessJourney.AWAY,
        )
    elif sys_status_name == PAUSE_MODE:
        _set_dock_access_journey(entity, DockAccessJourney.AWAY)
    elif sys_status_name == RETURNING_MODE:
        # MODE_RETURNING is authoritative. Dock fields often lag or arrive
        # out of order, so they cannot complete the journey by themselves.
        _clear_dock_access_arrival_candidate(entity)
    elif sys_status_name in ARRIVAL_CONFIRMATION_MODES:
        if _dock_access_arrival_is_stable(entity, values):
            _set_dock_access_journey(entity, DockAccessJourney.ARRIVED)
    else:
        _clear_dock_access_arrival_candidate(entity)


def _transition_dock_access_journey(
    entity: "MammotionBinarySensorEntity", values: dict[str, Any]
) -> None:
    """Apply the transition policy for the current journey state."""
    journey = entity._dock_access_journey
    if journey == DockAccessJourney.ARRIVED:
        _transition_dock_access_from_arrived(entity, values)
    elif journey == DockAccessJourney.DEPARTING:
        _transition_dock_access_from_departing(entity, values)
    elif journey == DockAccessJourney.AWAY:
        _transition_dock_access_from_away(entity, values)
    elif journey == DockAccessJourney.RETURNING:
        _transition_dock_access_from_returning(entity, values)


def _dock_access_requested(
    entity: "MammotionBinarySensorEntity", mower_data: MowingDevice
) -> bool | None:
    values = _raw_dock_access_values(mower_data)
    docked_or_charging = _is_docked_or_charging(values)
    now = time.monotonic()

    if entity._dock_access_journey is None:
        _set_dock_access_journey(entity, _initial_dock_access_journey(values))
        if (
            entity._dock_access_journey == DockAccessJourney.ARRIVED
            and docked_or_charging
        ):
            entity._dock_access_last_docked_or_charging_at = now
        return entity._dock_access_journey in (
            DockAccessJourney.DEPARTING,
            DockAccessJourney.RETURNING,
        )

    _transition_dock_access_journey(entity, values)
    if (
        docked_or_charging
        and entity._dock_access_journey == DockAccessJourney.ARRIVED
    ):
        entity._dock_access_last_docked_or_charging_at = now

    return entity._dock_access_journey in (
        DockAccessJourney.DEPARTING,
        DockAccessJourney.RETURNING,
    )


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
    journey = entity._dock_access_journey
    journey_started_at = entity._dock_access_journey_started_at
    arrival_candidate_since = entity._dock_access_arrival_candidate_since
    values["journey_state"] = None if journey is None else journey.value
    values["journey_seconds"] = (
        None
        if journey_started_at is None
        else max(0, int(time.monotonic() - journey_started_at))
    )
    values["arrival_candidate_seconds"] = (
        None
        if arrival_candidate_since is None
        else max(0, int(time.monotonic() - arrival_candidate_since))
    )
    # Preserve the original diagnostic attributes for existing dashboards.
    values["return_latched"] = journey == DockAccessJourney.RETURNING
    values["return_latch_satisfied"] = journey == DockAccessJourney.ARRIVED
    values["departure_seen_away"] = journey == DockAccessJourney.AWAY
    values["return_latched_docked_seconds"] = values[
        "arrival_candidate_seconds"
    ]
    last_docked_at = entity._dock_access_last_docked_or_charging_at
    values["last_trusted_docked_seconds"] = (
        None
        if last_docked_at is None
        else max(0, int(time.monotonic() - last_docked_at))
    )
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
    _dock_access_journey: DockAccessJourney | None
    _dock_access_journey_started_at: float | None
    _dock_access_arrival_candidate_since: float | None
    _dock_access_last_docked_or_charging_at: float | None
    _dock_access_state: bool | None
    _dock_access_state_changed_at: float | None
    _dock_access_close_pending_since: float | None
    _dock_access_journey_reevaluate_cancel: CALLBACK_TYPE | None
    _dock_access_state_reevaluate_cancel: CALLBACK_TYPE | None
    _dock_access_logged_initial_state: bool
    _dock_access_last_logged_state: bool | None

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
        self._dock_access_journey = None
        self._dock_access_journey_started_at = None
        self._dock_access_arrival_candidate_since = None
        self._dock_access_last_docked_or_charging_at = None
        self._dock_access_state = None
        self._dock_access_state_changed_at = None
        self._dock_access_close_pending_since = None
        self._dock_access_journey_reevaluate_cancel = None
        self._dock_access_state_reevaluate_cancel = None
        self._dock_access_logged_initial_state = False
        self._dock_access_last_logged_state = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending dock access timers before removal."""
        self._cancel_dock_access_journey_reevaluation()
        self._cancel_dock_access_state_reevaluation()
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self.entity_description.key == "dock_access_requested":
            if self._dock_access_state is None and self.coordinator.data is not None:
                self._update_dock_access_state()
            return self._dock_access_state
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
            self._update_dock_access_state()
            self._log_dock_access_transition()
        super()._handle_coordinator_update()

    def _update_dock_access_state(self) -> None:
        """Update the cached dock access state once per coordinator update."""
        if self.coordinator.data is None:
            self._cancel_dock_access_journey_reevaluation()
            self._cancel_dock_access_state_reevaluation()
            self._dock_access_journey = None
            self._dock_access_journey_started_at = None
            self._dock_access_arrival_candidate_since = None
            self._dock_access_state = None
            self._dock_access_state_changed_at = None
            self._dock_access_close_pending_since = None
            return
        requested = self.entity_description.is_on_fn(self, self.coordinator.data)
        now = time.monotonic()

        if self._dock_access_state is None:
            self._dock_access_state = requested
            self._dock_access_state_changed_at = now
            self._dock_access_close_pending_since = None
            return

        if requested == self._dock_access_state:
            self._dock_access_close_pending_since = None
            if requested:
                self._cancel_dock_access_state_reevaluation()
            return

        if self._dock_access_state and not requested:
            if self._dock_access_close_pending_since is None:
                self._dock_access_close_pending_since = now
            changed_at = self._dock_access_state_changed_at
            if (
                changed_at is not None
                and now - changed_at < DOCK_ACCESS_MIN_REQUEST_SECONDS
            ):
                self._schedule_dock_access_state_reevaluation(
                    DOCK_ACCESS_MIN_REQUEST_SECONDS - (now - changed_at)
                )
                return
            if (
                now - self._dock_access_close_pending_since
                < DOCK_ACCESS_CLOSE_DEBOUNCE_SECONDS
            ):
                self._schedule_dock_access_state_reevaluation(
                    DOCK_ACCESS_CLOSE_DEBOUNCE_SECONDS
                    - (now - self._dock_access_close_pending_since)
                )
                return

        if requested:
            self._dock_access_close_pending_since = None
            self._cancel_dock_access_state_reevaluation()
        self._dock_access_state = requested
        self._dock_access_state_changed_at = now
        if not requested:
            self._dock_access_close_pending_since = None
            self._cancel_dock_access_state_reevaluation()

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
            "access_phase=%s journey_seconds=%s arrival_candidate_seconds=%s "
            "last_trusted_docked_seconds=%s "
            "last_report_age_seconds=%s source_hint=%s)",
            self.coordinator.device_name,
            is_on,
            attrs["sys_status"],
            attrs["sys_status_name"],
            attrs["charge_state"],
            attrs["position_type"],
            attrs["position_type_name"],
            attrs["work_zone"],
            attrs["access_phase"],
            attrs["journey_seconds"],
            attrs["arrival_candidate_seconds"],
            attrs["last_trusted_docked_seconds"],
            attrs["last_report_age_seconds"],
            attrs["source_hint"],
        )

    def _schedule_dock_access_journey_reevaluation(self, delay: float) -> None:
        """Schedule a journey reevaluation after a journey timer expires."""
        if self.entity_description.key != "dock_access_requested":
            return
        if self._dock_access_journey_reevaluate_cancel is not None:
            return
        self._dock_access_journey_reevaluate_cancel = async_call_later(
            self.hass,
            max(0, delay),
            self._async_dock_access_journey_reevaluate,
        )

    def _cancel_dock_access_journey_reevaluation(self) -> None:
        """Cancel a pending dock journey reevaluation timer."""
        if self._dock_access_journey_reevaluate_cancel is None:
            return
        self._dock_access_journey_reevaluate_cancel()
        self._dock_access_journey_reevaluate_cancel = None

    def _schedule_dock_access_state_reevaluation(self, delay: float) -> None:
        """Schedule a published-state reevaluation after a hold expires."""
        if self.entity_description.key != "dock_access_requested":
            return
        if self._dock_access_state_reevaluate_cancel is not None:
            return
        self._dock_access_state_reevaluate_cancel = async_call_later(
            self.hass,
            max(0, delay),
            self._async_dock_access_state_reevaluate,
        )

    def _cancel_dock_access_state_reevaluation(self) -> None:
        """Cancel a pending published-state reevaluation timer."""
        if self._dock_access_state_reevaluate_cancel is None:
            return
        self._dock_access_state_reevaluate_cancel()
        self._dock_access_state_reevaluate_cancel = None

    def _reevaluate_dock_access(self) -> None:
        """Reevaluate dock access using the latest cached coordinator data."""
        if self.coordinator.data is None:
            return
        self._update_dock_access_state()
        self._log_dock_access_transition()
        self.async_write_ha_state()

    @callback
    def _async_dock_access_journey_reevaluate(self, _: datetime) -> None:
        """Reevaluate after a dock journey timer expires."""
        self._dock_access_journey_reevaluate_cancel = None
        self._reevaluate_dock_access()

    @callback
    def _async_dock_access_state_reevaluate(self, _: datetime) -> None:
        """Reevaluate after a published-state hold timer expires."""
        self._dock_access_state_reevaluate_cancel = None
        self._reevaluate_dock_access()
