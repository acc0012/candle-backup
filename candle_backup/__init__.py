# candle_backup/__init__.py
"""Standalone candle backup tool.

Fetches 1-minute candles for every subscribed instrument, computes EMA
crosses and support/resistance levels, and uploads the local ``data/``
directory to MEGA.
"""

__version__ = "1.1.0"