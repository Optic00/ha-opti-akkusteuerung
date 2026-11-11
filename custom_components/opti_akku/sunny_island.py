"""Read-only SMA Sunny Island profile for a first device test (#194).

Same approach as the Sunny Boy Storage profile (#170): read the addresses the
SMA Modbus profile assigns to the parameters used for the STP SE, plus
community evidence for the Sunny Island. Sources:

- evcc template ``sma-si-modbus`` (MIT): 30775 as the battery inverter's AC
  power, 30845 SoC, control only through 40149 and 40151.
- ioBroker project Maverick78de/SMA_forecast_charging (MIT), register list
  ``SIx.xx-12ff``: 30845, 30775, 31009, 31007, 30867, 30865, 30053, 30783,
  40035, 40149, 40151, 40189, 40191, 40236, 40795, 40799, 40801.
- Issue #194 (SI 6.0H-13, reporter quoting the SMA Modbus list): 40029
  OperMode with 303 off, 569 run, 1295 standby, 1795 locked; 30853; 40210
  operating mode of the active power setpoint, which the reporter had to set
  to 1079 (external setpoint) before the device followed 40149 and 40151.

None of these reads has been verified on a Sunny Island with this
integration yet. The profile never writes; the control registers are only
read back so a tester can see whether the device exposes them.
"""

from __future__ import annotations

from typing import Any

from .sbs import PROFILE_SMA_SBS, REGISTER_PROBE, SbsDevice

PROFILE_SMA_SUNNY_ISLAND = "sma_sunny_island"
# Operation.RunStt as on the STP SE first: it would report the codes the rest
# of the integration knows. Then OperMode from #194, then the SBS 2.5 register.
STATUS_REGISTERS = (33003, 40029, 33001)
# 569 = OperMode "run" (#194). Only readiness display; this profile never writes.
ALLOWED_STATUSES = (235, 2119, 569)
SI_REGISTER_PROBE = (
    (40029, False, "issue_194"),
    (30853, False, "issue_194"),
    (30783, False, "maverick_si"),
    (31007, False, "maverick_si"),
    (31009, False, "maverick_si"),
    (40149, True, "maverick_si_evcc"),
    (40151, False, "maverick_si_evcc"),
    (40210, False, "issue_194"),
    (40236, False, "maverick_si"),
    (40795, False, "maverick_si"),
    (40799, False, "maverick_si"),
    (40801, True, "maverick_si"),
)
_SI_ADDRESSES = {row[0] for row in SI_REGISTER_PROBE}


class SunnyIslandDevice(SbsDevice):
    """One SMA Sunny Island, strictly read-only."""

    profile = PROFILE_SMA_SUNNY_ISLAND
    model_label = "SMA Sunny Island"
    status_registers = STATUS_REGISTERS
    # RunStt and OperMode use different codes; RunStt must win whenever it answers.
    status_register_sticky = False
    allowed_statuses = ALLOWED_STATUSES
    probe_registers = (
        *(row for row in REGISTER_PROBE if row[0] not in _SI_ADDRESSES),
        *SI_REGISTER_PROBE,
    )

    @staticmethod
    def _derive(readings: dict[str, Any]) -> dict[str, Any]:
        """Fall back to the AC power when the charge/discharge registers are missing.

        evcc reads 30775 as the Sunny Island's battery power, positive when
        discharging. Opti counts charging as positive, hence the sign change.
        This is AC power at the inverter, not DC power at the battery.
        """
        readings = SbsDevice._derive(readings)
        ac_power = readings["sensor.opti_pv_power_w"]
        if readings["sensor.opti_battery_power_w"] is None and ac_power is not None:
            readings["sensor.opti_battery_power_w"] = -ac_power
        return readings


# Profiles without a control path: always shadow, never offered for active control.
READ_ONLY_SMA_PROFILES: dict[str, type[SbsDevice]] = {
    PROFILE_SMA_SBS: SbsDevice,
    PROFILE_SMA_SUNNY_ISLAND: SunnyIslandDevice,
}
