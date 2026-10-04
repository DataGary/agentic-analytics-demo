"""Forecasting: Holt-Winters exponential smoothing on a metric's history.

History is pulled through the semantic layer with the caller's token, so a regional manager
forecasts only their own region and an aggregates-only user can't forecast at daily grain.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

SEASONAL_PERIODS = {"day": 7, "week": 52, "month": 12}
FREQ = {"day": "D", "week": "W-MON", "month": "MS"}
Z80 = 1.2816


def _bucket_end(start: pd.Timestamp, granularity: str) -> date:
    if granularity == "day":
        return start.date()
    if granularity == "week":
        return (start + timedelta(days=6)).date()
    return (start + pd.offsets.MonthEnd(0)).date()


def _fit(y: np.ndarray, granularity: str):
    sp = SEASONAL_PERIODS[granularity]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if len(y) >= 2 * sp + 2:
            try:
                return ExponentialSmoothing(y, trend="add", seasonal="add", seasonal_periods=sp).fit(), f"Holt-Winters additive trend + seasonality ({sp})"
            except (ValueError, np.linalg.LinAlgError):
                pass
        return ExponentialSmoothing(y, trend="add").fit(), "Holt linear trend (not enough history for seasonality)"


def forecast_series(periods: list[str], values: list[float], granularity: str, horizon: int, today: date | None = None) -> dict:
    s = pd.Series(values, index=pd.to_datetime(periods)).sort_index()
    today = today or date.today()
    # Drop a trailing bucket that hasn't finished yet; it would look like a sudden drop.
    while len(s) and _bucket_end(s.index[-1], granularity) >= today:
        s = s.iloc[:-1]
    if len(s) < 8:
        raise ValueError(f"not enough history to forecast ({len(s)} complete {granularity} periods)")

    y = s.to_numpy(dtype=float)
    fit, model_name = _fit(y, granularity)
    pred = fit.forecast(horizon)
    resid_sd = float(np.std(y - fit.fittedvalues, ddof=1))

    # Backtest: refit without the last `horizon` points and score against what actually happened.
    h = min(horizon, len(y) // 4)
    bt_fit, _ = _fit(y[:-h], granularity)
    bt = bt_fit.forecast(h)
    mape = float(np.mean(np.abs((y[-h:] - bt) / np.where(y[-h:] == 0, 1, y[-h:])))) * 100

    future = pd.date_range(s.index[-1], periods=horizon + 1, freq=FREQ[granularity])[1:]
    return {
        "granularity": granularity,
        "model": model_name,
        "history_points": len(y),
        "last_actuals": [{"period": d.date().isoformat(), "value": round(float(v), 2)} for d, v in s.tail(4).items()],
        "forecast": [
            {
                "period": d.date().isoformat(),
                "value": round(float(v), 2),
                "lower_80": round(float(v - Z80 * resid_sd * np.sqrt(i + 1)), 2),
                "upper_80": round(float(v + Z80 * resid_sd * np.sqrt(i + 1)), 2),
            }
            for i, (d, v) in enumerate(zip(future, pred))
        ],
        "backtest_mape_pct": round(mape, 1),
    }
