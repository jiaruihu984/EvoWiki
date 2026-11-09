"""Offline eval set + judge calibration on top of the harness RSI (spec 2026-10-28
offline-eval-and-judge-calibration). The harness's own replay (`harness.replay`) writes, its blind
judge (`harness.judge.compare`) compares; this package adds what a per-user round lacks: a fixed
case set split by topic, several samples per case, a case-level bootstrap rule with an A/A noise
floor, a held-out check, judge calibration metrics, and a simulation that weighs each step."""
