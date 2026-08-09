"""Milestone 2: the stateful freight ledger.

Distinct from `ledger.py` (a stateless check over a selected job *set*), this is
the mutable execution ledger that tracks where every freight unit physically is
as jobs are applied over the horizon. One freight unit corresponds to one
commercial order (``freight_id == order_id``).

The single hard invariant it enforces: freight cannot be delivered from a depot
unless it is physically there — either prestaged before the horizon or produced
by an earlier pickup. Consuming absent freight raises ``FreightUnavailableError``
rather than silently going negative. This is the mechanism that makes phantom
crossdock deliveries impossible by construction.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

FREIGHT_NOT_READY = "NOT_READY"
FREIGHT_AT_CUSTOMER_ORIGIN = "AT_CUSTOMER_ORIGIN"
FREIGHT_ON_VEHICLE = "ON_VEHICLE"
FREIGHT_AT_DEPOT = "AT_DEPOT"
FREIGHT_AT_HUB = "AT_HUB"
FREIGHT_DELIVERED = "DELIVERED"

# Initial freight states (from state.build_initial_freight_states) that mean the
# freight is already staged at a depot/hub before the planning horizon opens.
_PRESTAGED_INITIAL_STATES = {"AT_DEPOT_OR_HUB_PENDING", FREIGHT_AT_DEPOT, FREIGHT_AT_HUB}
_ORIGIN_INITIAL_STATES = {FREIGHT_AT_CUSTOMER_ORIGIN}


class FreightUnavailableError(Exception):
    """Raised when freight is consumed from a node where it does not exist."""


@dataclass
class FreightUnit:
    freight_id: str
    state: str
    depot: str = ""


class FreightLedger:
    """Tracks the physical location/state of each freight unit."""

    def __init__(self) -> None:
        self.units: dict[str, FreightUnit] = {}

    @classmethod
    def from_initial_states(cls, freight_states: pd.DataFrame) -> "FreightLedger":
        led = cls()
        if freight_states is None or freight_states.empty:
            return led
        for row in freight_states.itertuples(index=False):
            freight_id = str(getattr(row, "freight_id", "") or getattr(row, "order_id", "") or "")
            if not freight_id:
                continue
            initial_state = str(getattr(row, "initial_state", "") or "")
            depot = str(getattr(row, "initial_depot", "") or "")
            if initial_state in _PRESTAGED_INITIAL_STATES:
                led.register(freight_id, FREIGHT_AT_DEPOT, depot)
            elif initial_state in _ORIGIN_INITIAL_STATES:
                led.register(freight_id, FREIGHT_AT_CUSTOMER_ORIGIN, depot)
            # Manual/out-of-scope freight is not registered as dispatchable.
        return led

    def register(self, freight_id: str, state: str, depot: str = "") -> FreightUnit:
        unit = FreightUnit(freight_id=str(freight_id), state=str(state), depot=str(depot))
        self.units[unit.freight_id] = unit
        return unit

    def get(self, freight_id: str) -> FreightUnit | None:
        return self.units.get(str(freight_id))

    def state_of(self, freight_id: str) -> str:
        unit = self.get(freight_id)
        return unit.state if unit is not None else ""

    def exists_at_depot(self, freight_id: str, depot: str = "") -> bool:
        unit = self.get(freight_id)
        if unit is None or unit.state != FREIGHT_AT_DEPOT:
            return False
        return depot == "" or unit.depot == str(depot)

    def depot_inventory(self, depot: str) -> set[str]:
        return {
            fid for fid, unit in self.units.items()
            if unit.state == FREIGHT_AT_DEPOT and unit.depot == str(depot)
        }

    def pickup_to_depot(self, freight_id: str, depot: str) -> FreightUnit:
        """A customer pickup produces depot freight at ``depot``."""
        unit = self.get(freight_id)
        if unit is None:
            unit = self.register(freight_id, FREIGHT_AT_CUSTOMER_ORIGIN)
        if unit.state == FREIGHT_DELIVERED:
            raise FreightUnavailableError(
                f"freight {freight_id!r} already delivered; cannot pick up again"
            )
        unit.state = FREIGHT_AT_DEPOT
        unit.depot = str(depot)
        return unit

    def deliver_from_depot(self, freight_id: str, depot: str) -> FreightUnit:
        """Consume depot freight for a final delivery."""
        if not self.exists_at_depot(freight_id, depot):
            raise FreightUnavailableError(
                f"freight {freight_id!r} not available at depot {depot!r}"
            )
        unit = self.get(freight_id)
        assert unit is not None  # guarded by exists_at_depot
        unit.state = FREIGHT_DELIVERED
        unit.depot = ""
        return unit

    def handoff_to_hub(self, freight_id: str) -> FreightUnit:
        """A hub-drop hands export freight to the Palletline/Hazchem network at the
        hub in one leg. It consumes origin freight and is terminal for our books
        (the network owns it onward). Tolerant of an unregistered origin like
        ``pickup_to_depot``; rejects re-handling already-delivered freight."""
        unit = self.get(freight_id)
        if unit is None:
            unit = self.register(freight_id, FREIGHT_AT_CUSTOMER_ORIGIN)
        if unit.state == FREIGHT_DELIVERED:
            raise FreightUnavailableError(
                f"freight {freight_id!r} already delivered; cannot hand to hub"
            )
        unit.state = FREIGHT_DELIVERED
        unit.depot = ""
        return unit

    def deliver_direct(self, freight_id: str) -> FreightUnit:
        """A direct customer-to-customer move consumes origin freight in one leg."""
        unit = self.get(freight_id)
        if unit is None or unit.state not in (FREIGHT_AT_CUSTOMER_ORIGIN, FREIGHT_ON_VEHICLE):
            raise FreightUnavailableError(
                f"freight {freight_id!r} not available at customer origin for direct move"
            )
        unit.state = FREIGHT_DELIVERED
        unit.depot = ""
        return unit
