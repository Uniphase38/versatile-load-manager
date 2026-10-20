"""Crash-safe ownership rules for temporary ESS minimum-SOC overrides."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MinSocOwnership:
    owned: bool = False
    original_soc: Optional[float] = None


@dataclass(frozen=True)
class MinSocAction:
    action: str
    value: Optional[float]
    ownership: MinSocOwnership


def plan_min_soc_action(
    *,
    desired_soc: Optional[float],
    observed_soc: float,
    ownership: MinSocOwnership,
) -> MinSocAction:
    """Plan capture/set/restore without guessing the pre-existing value."""
    if not 0 <= observed_soc <= 100:
        raise ValueError("observed minimum SOC must be between 0 and 100")
    if desired_soc is not None and not 0 <= desired_soc <= 100:
        raise ValueError("desired minimum SOC must be between 0 and 100")

    if desired_soc is not None:
        if not ownership.owned:
            return MinSocAction("capture", None, MinSocOwnership(True, observed_soc))
        if ownership.original_soc is None:
            return MinSocAction("fault", None, ownership)
        if observed_soc != desired_soc:
            return MinSocAction("set", desired_soc, ownership)
        return MinSocAction("none", None, ownership)

    if not ownership.owned:
        return MinSocAction("none", None, ownership)
    if ownership.original_soc is None:
        return MinSocAction("fault", None, ownership)
    if observed_soc != ownership.original_soc:
        return MinSocAction("restore", ownership.original_soc, ownership)
    return MinSocAction("release", None, MinSocOwnership())
