"""Offline replay harness for the Opti battery strategy.

Replays recorded hourly plant data through the real ``StrategyEngine`` (or a
baseline policy) against a simple battery model, and compares the resulting
grid flows, costs and decision reasons between strategy revisions. Everything
here is a tool for analysis; it never changes the shipped strategy.
"""
