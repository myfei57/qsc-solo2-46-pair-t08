"""Domain failures raised by the control service.

Every failure carries a stable type so the HTTP layer can map it to a status
code without inspecting message text.
"""

from __future__ import annotations


class CraneFleetError(Exception):
    """Base class for every failure raised by the control service."""


class StoreError(CraneFleetError):
    """Raised when the persistence layer cannot satisfy a request."""


class StoreNotFound(StoreError):
    """Raised when a store key has never been written."""


class ZoneError(CraneFleetError):
    """Raised when a collision-zone operation is rejected."""


class ZoneNotDurable(ZoneError):
    """Raised when a swing is attempted before the zone state is persisted."""


class ClusterError(CraneFleetError):
    """Raised when a fleet-level operation is rejected."""


class NoCranesSurveyed(ClusterError):
    """Raised when a baseline refresh runs before any crane is surveyed."""


class SlewError(CraneFleetError):
    """Base class for slewing failures."""


class SlewBlocked(SlewError):
    """Raised when an interlock (stop, wind or latch) holds the slew."""


class SlewBusy(SlewError):
    """Raised when a swing starts while the slew is already moving."""


class SlewIdle(SlewError):
    """Raised when a swing is applied while the slew is not moving."""


class TrolleyError(CraneFleetError):
    """Base class for trolley failures."""


class HookNotLowered(TrolleyError):
    """Raised when the trolley moves before the hook is lowered."""


class HoistError(CraneFleetError):
    """Base class for hoist failures."""


class HoistBusy(HoistError):
    """Raised when the drum is already turning."""


class FeedbackNotConfirmed(HoistError):
    """Raised when the brake is released before the speed feedback arrives."""


class WindError(CraneFleetError):
    """Base class for wind failures."""


class SlewStopNotAcknowledged(WindError):
    """Raised when the slew stop is not acknowledged during a wind alarm."""


class LimitError(CraneFleetError):
    """Base class for limit failures."""


class StaleRelease(LimitError):
    """Raised when a stale generation tries to release the limit latch."""


class ConfigError(CraneFleetError):
    """Raised when a site configuration payload is invalid."""


class RegistryError(CraneFleetError):
    """Base class for equipment registry failures."""


class RegistryValidationError(RegistryError):
    """Raised when an equipment payload or timestamp fails validation."""


class EquipmentExists(RegistryError):
    """Raised when a registered equipment id or serial number is already on file."""


class EquipmentNotFound(RegistryError):
    """Raised when an operation references an unregistered equipment record."""


class SlotOccupied(RegistryError):
    """Raised when a machine is mounted onto a slot that already runs one."""


class SlotEmpty(RegistryError):
    """Raised when an empty slot is asked to swap or unmount."""


class EquipmentMountedElsewhere(RegistryError):
    """Raised when a mounted machine is assigned to a second slot."""
