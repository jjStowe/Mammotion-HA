"""Tests for Mammotion binary sensors."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from custom_components.mammotion import binary_sensor as binary_sensor_module
from custom_components.mammotion.binary_sensor import (
    BINARY_SENSORS,
    DockAccessJourney,
    MammotionBinarySensorEntity,
)


class FakeClock:
    """Provide a controllable monotonic clock."""

    def __init__(self) -> None:
        """Initialize the clock at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current fake monotonic time."""
        return self.now


class FakeDockAccessEntity(MammotionBinarySensorEntity):
    """Exercise production journey and state logic without Home Assistant."""

    def __init__(self, clock: FakeClock) -> None:
        """Initialize only the state needed by the dock-access policy."""
        self.clock = clock
        self.entity_description = next(
            description
            for description in BINARY_SENSORS
            if description.key == "dock_access_requested"
        )
        self.coordinator = SimpleNamespace(data=None)
        self._dock_access_journey: DockAccessJourney | None = None
        self._dock_access_journey_started_at: float | None = None
        self._dock_access_arrival_candidate_since: float | None = None
        self._dock_access_last_docked_or_charging_at: float | None = None
        self._dock_access_state: bool | None = None
        self._dock_access_state_changed_at: float | None = None
        self._dock_access_close_pending_since: float | None = None
        self._dock_access_journey_reevaluate_cancel = None
        self._dock_access_state_reevaluate_cancel = None
        self.journey_timer_due: float | None = None
        self.state_timer_due: float | None = None

    @property
    def journey(self) -> DockAccessJourney | None:
        """Return the current dock journey."""
        return self._dock_access_journey

    @property
    def access_requested(self) -> bool | None:
        """Return the published access state."""
        return self._dock_access_state

    @property
    def arrival_candidate_since(self) -> float | None:
        """Return the start of stable arrival evidence."""
        return self._dock_access_arrival_candidate_since

    def update(self, at: float, mower_data: SimpleNamespace | None) -> None:
        """Evaluate a mower snapshot at a fake monotonic time."""
        self.clock.now = at
        self.coordinator.data = mower_data
        self._update_dock_access_state()

    def fire_journey_timer(self, at: float) -> None:
        """Fire the pending journey timer at the requested time."""
        assert self.journey_timer_due is not None
        assert at >= self.journey_timer_due
        self.clock.now = at
        self.journey_timer_due = None
        self._update_dock_access_state()

    def fire_state_timer(self, at: float) -> None:
        """Fire the pending published-state timer at the requested time."""
        assert self.state_timer_due is not None
        assert at >= self.state_timer_due
        self.clock.now = at
        self.state_timer_due = None
        self._update_dock_access_state()

    def _schedule_dock_access_journey_reevaluation(self, delay: float) -> None:
        """Record the first pending journey reevaluation."""
        if self.journey_timer_due is None:
            self.journey_timer_due = self.clock.now + max(0, delay)

    def _cancel_dock_access_journey_reevaluation(self) -> None:
        """Cancel the pending journey reevaluation."""
        self.journey_timer_due = None

    def _schedule_dock_access_state_reevaluation(self, delay: float) -> None:
        """Record the first pending published-state reevaluation."""
        if self.state_timer_due is None:
            self.state_timer_due = self.clock.now + max(0, delay)

    def _cancel_dock_access_state_reevaluation(self) -> None:
        """Cancel the pending published-state reevaluation."""
        self.state_timer_due = None


def _mower_data(
    sys_status: int, *, charge_state: int = 0, position_type: int | None = 1
) -> SimpleNamespace:
    """Build the subset of mower telemetry used by dock-access logic."""
    return SimpleNamespace(
        report_data=SimpleNamespace(
            dev=SimpleNamespace(
                sys_status=sys_status,
                charge_state=charge_state,
            )
        ),
        location=SimpleNamespace(position_type=position_type),
        mowing_state=SimpleNamespace(zone_hash=None),
    )


def _entity() -> tuple[FakeClock, FakeDockAccessEntity, Any]:
    """Create a test entity and patch its monotonic clock."""
    clock = FakeClock()
    entity = FakeDockAccessEntity(clock)
    clock_patch = patch.object(binary_sensor_module.time, "monotonic", clock)
    return clock, entity, clock_patch


def test_last_report_age_uses_monotonic_report_timestamp() -> None:
    """Verify report age compares two monotonic clock values."""
    handle = SimpleNamespace(last_report_data_at=40.25)
    coordinator = SimpleNamespace(
        device_name="test-mower",
        manager=SimpleNamespace(mower=lambda _: handle),
    )

    with patch.object(binary_sensor_module.time, "monotonic", return_value=45.75):
        assert binary_sensor_module._last_report_age_seconds(coordinator) == 5  # noqa: SLF001


def test_last_report_age_is_unknown_before_first_report() -> None:
    """Verify a zero monotonic timestamp means no report has arrived."""
    handle = SimpleNamespace(last_report_data_at=0.0)
    coordinator = SimpleNamespace(
        device_name="test-mower",
        manager=SimpleNamespace(mower=lambda _: handle),
    )

    assert binary_sensor_module._last_report_age_seconds(coordinator) is None  # noqa: SLF001


def test_returning_ignores_stale_docked_telemetry() -> None:
    """Verify MODE_RETURNING remains authoritative despite stale dock fields."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(11, charge_state=1, position_type=5))
        entity.update(1, _mower_data(14, charge_state=1, position_type=5))
        entity.update(30, _mower_data(14, charge_state=1, position_type=5))
        entity.update(120, _mower_data(14, charge_state=1, position_type=5))

    assert entity.journey == DockAccessJourney.RETURNING
    assert entity.access_requested
    assert entity.arrival_candidate_since is None
    assert entity.journey_timer_due is None


def test_arrival_requires_stable_docked_mode() -> None:
    """Verify arrival requires 20 seconds of credible docked-mode evidence."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(70, _mower_data(15, charge_state=1, position_type=5))

        assert entity.journey == DockAccessJourney.RETURNING
        assert entity.journey_timer_due == 90

        entity.fire_journey_timer(90)
        assert entity.journey == DockAccessJourney.ARRIVED
        assert entity.access_requested
        assert entity.state_timer_due == 105

        entity.fire_state_timer(105)

    assert not entity.access_requested


def test_returning_report_cancels_arrival_candidate() -> None:
    """Verify MODE_RETURNING cancels mixed-order dock arrival evidence."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(70, _mower_data(15, charge_state=1, position_type=5))
        entity.update(80, _mower_data(14, charge_state=1, position_type=5))

    assert entity.journey == DockAccessJourney.RETURNING
    assert entity.arrival_candidate_since is None
    assert entity.journey_timer_due is None
    assert entity.access_requested


def test_cancelled_return_does_not_reopen_on_pause() -> None:
    """Verify canceling a return clears the return journey permanently."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(61, _mower_data(19, charge_state=1, position_type=5))

        assert entity.journey == DockAccessJourney.AWAY
        assert entity.access_requested
        assert entity.state_timer_due == 76

        entity.fire_state_timer(76)
        entity.update(77, _mower_data(19, charge_state=1, position_type=5))

    assert entity.journey == DockAccessJourney.AWAY
    assert not entity.access_requested


def test_pause_and_resume_preserve_departure_grace() -> None:
    """Verify a pause and resume cannot close access during departure grace."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(11, charge_state=1, position_type=5))
        entity.update(1, _mower_data(13, charge_state=1, position_type=5))
        entity.update(30, _mower_data(19))
        entity.update(60, _mower_data(13))

        assert entity.journey == DockAccessJourney.DEPARTING
        assert entity.access_requested
        assert entity.journey_timer_due == 91

        entity.fire_journey_timer(91)
        assert entity.journey == DockAccessJourney.AWAY
        assert entity.access_requested
        assert entity.state_timer_due == 106

        entity.fire_state_timer(106)

    assert not entity.access_requested


def test_complete_departure_and_return_journey() -> None:
    """Verify access opens and closes correctly across a complete journey."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(11, charge_state=1, position_type=5))
        entity.update(1, _mower_data(13, charge_state=1, position_type=5))
        entity.update(30, _mower_data(13))
        entity.fire_journey_timer(91)
        entity.fire_state_timer(106)

        assert entity.journey == DockAccessJourney.AWAY
        assert not entity.access_requested

        entity.update(200, _mower_data(14))
        entity.update(210, _mower_data(14, charge_state=1, position_type=5))
        entity.update(230, _mower_data(15, charge_state=1, position_type=5))
        entity.fire_journey_timer(250)
        entity.fire_state_timer(260)

        assert entity.access_requested
        assert entity.state_timer_due == 265

        entity.fire_state_timer(265)

    assert entity.journey == DockAccessJourney.ARRIVED
    assert not entity.access_requested


def test_delayed_recharge_reports_do_not_reopen_access() -> None:
    """Verify delayed recharge frames cannot reopen access during mowing."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(11, charge_state=1, position_type=5))
        entity.update(1, _mower_data(13, charge_state=1, position_type=5))
        entity.update(30, _mower_data(13))
        entity.fire_journey_timer(91)
        entity.fire_state_timer(106)

        assert entity.journey == DockAccessJourney.AWAY
        assert not entity.access_requested

        entity.update(120, _mower_data(39))
        entity.update(121, _mower_data(39, charge_state=1, position_type=5))
        entity.update(122, _mower_data(19, charge_state=1, position_type=5))
        entity.update(123, _mower_data(15, charge_state=1, position_type=5))
        entity.update(124, _mower_data(13))

    assert entity.journey == DockAccessJourney.AWAY
    assert not entity.access_requested
    assert entity.journey_timer_due is None
    assert entity.state_timer_due is None


def test_sustained_recharge_without_return_report_enables_resume() -> None:
    """Verify sustained dock evidence restores departure handling after recharge."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(19, charge_state=1, position_type=5))

        assert entity.journey == DockAccessJourney.AWAY
        assert not entity.access_requested
        assert entity.journey_timer_due == 61

        entity.fire_journey_timer(61)

        assert entity.journey == DockAccessJourney.ARRIVED
        assert not entity.access_requested

        entity.update(100, _mower_data(13, charge_state=1, position_type=5))

    assert entity.journey == DockAccessJourney.DEPARTING
    assert entity.access_requested
    assert entity.journey_timer_due == 190


def test_docked_pause_settles_as_implicit_arrival() -> None:
    """Verify docked pause ends a return before confirming stable arrival."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(70, _mower_data(19, charge_state=1, position_type=5))

        assert entity.journey == DockAccessJourney.AWAY
        assert entity.journey_timer_due == 130

        entity.fire_state_timer(85)
        entity.fire_journey_timer(130)

    assert entity.journey == DockAccessJourney.ARRIVED
    assert not entity.access_requested


def test_charging_pause_confirms_established_return() -> None:
    """Verify charging pause can finish an established return."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(70, _mower_data(39, charge_state=1, position_type=5))
        entity.fire_journey_timer(90)
        entity.fire_state_timer(105)

    assert entity.journey == DockAccessJourney.ARRIVED
    assert not entity.access_requested


def test_error_during_return_keeps_access_open() -> None:
    """Verify an unrelated error mode cannot abandon a confirmed return."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(13))
        entity.update(1, _mower_data(14))
        entity.update(120, _mower_data(17))

    assert entity.journey == DockAccessJourney.RETURNING
    assert entity.access_requested


def test_cold_start_classification() -> None:
    """Verify cold startup distinguishes docked, departing, and mid-yard states."""
    _, docked_return, docked_patch = _entity()
    with docked_patch:
        docked_return.update(0, _mower_data(14, charge_state=1, position_type=5))

    _, charge_only_departure, departure_patch = _entity()
    with departure_patch:
        charge_only_departure.update(
            0, _mower_data(13, charge_state=1, position_type=None)
        )

    _, mid_yard, mid_yard_patch = _entity()
    with mid_yard_patch:
        mid_yard.update(0, _mower_data(13))

    assert docked_return.journey == DockAccessJourney.ARRIVED
    assert not docked_return.access_requested
    assert charge_only_departure.journey == DockAccessJourney.DEPARTING
    assert charge_only_departure.access_requested
    assert mid_yard.journey == DockAccessJourney.AWAY
    assert not mid_yard.access_requested


def test_missing_coordinator_data_resets_journey_and_timers() -> None:
    """Verify loss of coordinator data clears all transient journey state."""
    _, entity, clock_patch = _entity()
    with clock_patch:
        entity.update(0, _mower_data(11, charge_state=1, position_type=5))
        entity.update(1, _mower_data(13, charge_state=1, position_type=5))
        entity.update(2, None)

    assert entity.journey is None
    assert entity.access_requested is None
    assert entity.journey_timer_due is None
    assert entity.state_timer_due is None
