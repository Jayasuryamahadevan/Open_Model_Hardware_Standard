"""A bounded arm simulator for the 'wave hello' MCP workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from ...core.capability import (
    Command,
    Constraint,
    Feature,
    Hazard,
    Parameter,
    Property,
    Reversibility,
)
from ...core.device import Device, DeviceDescriptor, ExecutionContext, SimulationResult
from ...core.errors import DeviceNotReady, ValidationError


class SimulatedRobotArm(Device):
    """A single shoulder joint with a bounded waving gesture, not a real arm driver."""

    def __init__(self, descriptor: DeviceDescriptor, **config: Any) -> None:
        super().__init__(descriptor, **config)
        self.homed = False
        self.angle_deg = 0.0
        self.last_gesture = "none"

    def _features(self) -> Sequence[Feature]:
        return [Feature(
            identifier="MotionControl", display_name="Arm motion",
            namespace="org.labbench.robotics",
            description="One simulated shoulder joint. Home before waving. Clear the workspace.",
            properties=[
                Property(name="homed", schema=Parameter(name="homed", type="boolean")),
                Property(name="angle_deg", schema=Parameter(name="angle_deg", unit="deg")),
                Property(name="last_gesture", schema=Parameter(name="last_gesture", type="string")),
            ],
            commands=[
                Command(name="home", description="Move to the calibrated zero pose.",
                        hazard=Hazard.MOTION, reversibility=Reversibility.RESTORABLE,
                        duration_estimate_s=0.1),
                Command(name="wave", description="Wave the arm as a greeting, then return to zero.",
                        parameters=[
                            Parameter(name="cycles", type="integer", default=2, required=False,
                                      constraint=Constraint(minimum=1, maximum=3)),
                            Parameter(name="amplitude_deg", unit="deg", default=20.0,
                                      required=False, constraint=Constraint(minimum=5, maximum=30)),
                        ], hazard=Hazard.MOTION, reversibility=Reversibility.RESTORABLE,
                        duration_estimate_s=0.3, tags={"greeting", "motion"}),
            ],
        )]

    async def _read(self, feature: str, name: str) -> Any:
        if name in ("homed", "angle_deg", "last_gesture"):
            return getattr(self, name)
        raise ValidationError(f"unknown property {name!r}")

    async def _invoke(self, feature: str, command: str, args: dict[str, Any],
                      ctx: ExecutionContext) -> dict[str, Any]:
        if command == "home":
            self.angle_deg = 0.0
            self.homed = True
            return {"homed": True, "angle_deg": 0.0}
        if command == "wave":
            if not self.homed:
                raise DeviceNotReady("home the arm before waving", device=self.id)
            cycles = args["cycles"]
            amplitude = args["amplitude_deg"]
            for _ in range(cycles):
                ctx.raise_if_cancelled()
                self.angle_deg = amplitude
                await asyncio.sleep(0)
                ctx.raise_if_cancelled()
                self.angle_deg = -amplitude
                await asyncio.sleep(0)
            self.angle_deg = 0.0
            self.last_gesture = "wave"
            return {"gesture": "wave", "cycles": cycles, "returned_to_zero": True}
        raise ValidationError(f"unknown command {command!r}")

    async def _simulate(self, feature: str, command: str,
                        args: dict[str, Any]) -> SimulationResult:
        if command == "home":
            return SimulationResult(feasible=True, fidelity="kinematic",
                                    predicted_state={"homed": True, "angle_deg": 0.0})
        if command == "wave":
            return SimulationResult(
                feasible=self.homed, fidelity="kinematic",
                predicted_state={"angle_deg": 0.0, "last_gesture": "wave"},
                violations=[] if self.homed else ["arm is not homed"],
                warnings=["Simulation checks joint bounds, not obstacles in a real workspace."],
            )
        raise ValidationError(f"unknown command {command!r}")

    async def _estop(self) -> None:
        self.angle_deg = 0.0
