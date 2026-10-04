from datetime import date

import numpy as np
import pandas as pd
import pytest

from mcp_server.tools.forecast import forecast_series


def synthetic_weekly(n=110, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-09-30", periods=n, freq="W-MON")
    t = np.arange(n)
    y = 100_000 + 300 * t + 15_000 * np.sin(2 * np.pi * t / 52) + rng.normal(0, 2_000, n)
    return [d.date().isoformat() for d in idx], y.tolist(), idx


def test_weekly_forecast_shape_and_accuracy():
    periods, values, idx = synthetic_weekly()
    out = forecast_series(periods, values, "week", horizon=8, today=date(2030, 1, 1))
    assert len(out["forecast"]) == 8
    assert out["forecast"][0]["period"] == (idx[-1] + pd.Timedelta(weeks=1)).date().isoformat()
    assert all(f["lower_80"] < f["value"] < f["upper_80"] for f in out["forecast"])
    assert "seasonality" in out["model"]
    assert out["backtest_mape_pct"] < 10


def test_incomplete_trailing_bucket_dropped():
    periods, values, idx = synthetic_weekly()
    today = (idx[-1] + pd.Timedelta(days=3)).date()  # last week not finished yet
    out = forecast_series(periods, values, "week", horizon=4, today=today)
    assert out["history_points"] == len(periods) - 1


def test_short_history_falls_back_to_trend():
    out = forecast_series([f"2026-0{m}-01" for m in range(1, 10)], [10, 12, 13, 15, 16, 18, 19, 21, 22],
                          "month", horizon=3, today=date(2027, 1, 1))
    assert "trend" in out["model"] and len(out["forecast"]) == 3


def test_too_little_history():
    with pytest.raises(ValueError, match="not enough history"):
        forecast_series(["2026-01-01", "2026-02-01"], [1, 2], "month", 3, today=date(2027, 1, 1))
