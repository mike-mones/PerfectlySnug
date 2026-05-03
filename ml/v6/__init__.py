"""ml.v6 — PerfectlySnug v6 ML modules.

Pure-Python modules implementing the v6 controller's ML stack:
- regime: deterministic regime classifier (§3 + §6 of recommendation.md)
- firmware_plant: forward predictor for firmware Stage-1+2 cascade
- right_comfort_proxy: composite right-zone comfort metric (§5)
- residual_head: bounded Bayesian Ridge + GP quorum learned residual (opt-learned §1)

These modules have no AppDaemon dependency and perform no live actuation.
They are imported by the v6 controller (R2A) and the eval harness (R1A).
"""
