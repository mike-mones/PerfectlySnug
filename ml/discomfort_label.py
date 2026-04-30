"""
Per-minute discomfort labeling for the PerfectlySnug ML controller.

Why this exists
---------------
The fundamental binding constraint on the v6 ML controller (PROGRESS_REPORT.md
§6) is *not* model architecture — it is the **biased preference signal**.
Override events (47 left-zone, ~2.4/night) only fire when v5 was wrong enough
that the user manually corrected. The other ~99% of minutes are silent. Naive
maximum-likelihood on overrides over-corrects (NEW MAE 2.13 vs v5 1.81).

User context (2026-04-29 conversation): the user has reported waking up COLD
mid-night without overriding. Those minutes are completely invisible to the
current learner. We need a proxy.

This module produces a *per-minute discomfort label* with a per-row weight,
mixing three sources:

  1. **Override events** (weight 1.0)            — ground truth, ~47/corpus
  2. **Proxy-discomfort minutes** (weight 0.3)   — fired by physiology/movement
                                                    candidate signals validated
                                                    against overrides as ground
                                                    truth (see §SIGNALS below).
  3. **Silent acceptance** (weight 0.05)         — every other occupied minute,
                                                    a weak vote of confidence in
                                                    the setting that was active.

Sources are mutually exclusive in priority order: override > proxy > silent.

SIGNALS (per `tools/build_discomfort_corpus.py` validation)
-----------------------------------------------------------
Each candidate is a per-minute boolean feature with a *5-minute consensus*
gate (must persist) and a *15-minute occupancy gate* (must be on bed).

  a. hr_spike            HR > rolling 30-min p90 of current night's sleeping HR
  b. hrv_dip             HRV < rolling 30-min p20
  c. rr_jump             Respiratory rate > rolling 30-min p90
  d. stage_fragmentation any awake/wake segment within trailing 30 min
  e. pressure_burst      bed pressure variance > rolling 30-min p90
  f. body_sd_q4          body_30m_sd in top quartile (proven 2.4× leading
                         indicator per analyze_discomfort_signals.py)
  g. combined            ≥2 of the above co-occurring within a 5-min window

The combined `discomfort_proxy` label is the AND of:
  - body_sd_q4   (the strongest single signal, AUC ~0.65 expected)
  - any of {hr_spike, hrv_dip, stage_fragmentation, pressure_burst}

Both must occur within the same 5-minute window. This combo is conservative
(designed for precision >0.3, recall >0.6 vs override events, target FPR
<5/night).

Public API
----------
    label_minute(row, *, prior, weights=DEFAULT_WEIGHTS) -> dict
        Pure-Python helper for a single per-minute row. Used by the live
        shadow logger (no numpy/pandas dependency).

    build_label_corpus(readings_per_min, *, weights=DEFAULT_WEIGHTS) -> DataFrame
        Vectorised DataFrame builder for offline training. Adds columns:
          discomfort_label  ∈ {0,1}
          label_weight      ∈ [0, 1]
          label_source      ∈ {'override','proxy','silent','empty'}
          proxy_signals     comma-joined names of fired candidates (for audit)

The minute-resolution DataFrame must already carry the candidate features
populated by `compute_candidate_signals()` (vectorised, in this module).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Heavy deps optional: label_minute() works without them.
try:  # pragma: no cover
    import numpy as np
    import pandas as pd
except ImportError:  # pragma: no cover
    np = None
    pd = None


# ── Configuration ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class LabelWeights:
    override: float = 1.00
    proxy: float = 0.30
    silent: float = 0.05
    empty: float = 0.00


DEFAULT_WEIGHTS = LabelWeights()

# Trailing window for the rolling per-night percentile gates (minutes).
ROLLING_WINDOW_MIN = 30

# Persistence gate: a candidate signal must be true for at least this many of
# the last `PERSISTENCE_WINDOW_MIN` minutes to count as "real" (debounce).
PERSISTENCE_WINDOW_MIN = 5
PERSISTENCE_MIN_HITS = 2

# Combined-proxy: require at least N candidates active within the same window.
COMBINED_MIN_SIGNALS = 2
COMBINED_WINDOW_MIN = 5

# Occupancy gate: ignore minutes where the bed-presence sensor says no body.
REQUIRE_OCCUPIED = True


# ── Per-minute label helper (pure python, deploy-safe) ─────────────────

def label_minute(*,
                 is_override: bool,
                 proxy_fired: bool,
                 occupied: bool,
                 weights: LabelWeights = DEFAULT_WEIGHTS) -> dict:
    """Resolve a single minute to (label, weight, source).

    Priority order: override > proxy > silent. Empty-bed minutes get weight 0
    so they cannot influence the fit.

    >>> label_minute(is_override=True,  proxy_fired=False, occupied=True)
    {'label': 1, 'weight': 1.0, 'source': 'override'}
    >>> label_minute(is_override=False, proxy_fired=True,  occupied=True)
    {'label': 1, 'weight': 0.3, 'source': 'proxy'}
    >>> label_minute(is_override=False, proxy_fired=False, occupied=True)
    {'label': 0, 'weight': 0.05, 'source': 'silent'}
    >>> label_minute(is_override=False, proxy_fired=True,  occupied=False)
    {'label': 0, 'weight': 0.0, 'source': 'empty'}
    """
    if REQUIRE_OCCUPIED and not occupied:
        return {"label": 0, "weight": weights.empty, "source": "empty"}
    if is_override:
        return {"label": 1, "weight": weights.override, "source": "override"}
    if proxy_fired:
        return {"label": 1, "weight": weights.proxy, "source": "proxy"}
    return {"label": 0, "weight": weights.silent, "source": "silent"}


# ── Vectorised candidate-signal computation (offline) ──────────────────

def _persistent(series, min_hits: int = PERSISTENCE_MIN_HITS,
                window_min: int = PERSISTENCE_WINDOW_MIN):
    """Debounce a boolean per-minute series: true iff fires ≥min_hits in window.

    Robust to a single-minute Apple-Watch glitch firing the candidate; we want
    *sustained* discomfort signals.
    """
    if pd is None:  # pragma: no cover
        raise ImportError("pandas required for build_label_corpus")
    return series.fillna(False).astype(int).rolling(
        f"{window_min}min", min_periods=1).sum().ge(min_hits)


def _rolling_pct_gate(series, window_min: int, percentile: float,
                      direction: str = "above"):
    """Per-minute boolean: value crosses the trailing per-night percentile.

    `series` is assumed indexed by ts (DatetimeIndex). NaN-safe; a minute with
    fewer than 6 prior valid observations returns False (insufficient stats).
    `direction` ∈ {'above','below'}.
    """
    win = series.rolling(f"{window_min}min", min_periods=6)
    thresh = win.quantile(percentile)
    if direction == "above":
        return series.gt(thresh).fillna(False)
    return series.lt(thresh).fillna(False)


def compute_candidate_signals(per_min: "pd.DataFrame") -> "pd.DataFrame":
    """Add boolean candidate-signal columns to a minute-resolution DataFrame.

    Required input columns (any may be NaN; signals fail-closed when missing):
      ts (DatetimeIndex), hr, hrv, rr, body_avg_f, pressure_pct,
      sleep_stage (str: 'awake'|'core'|'deep'|'rem'|'unknown'),
      occupied (bool).

    Output columns added:
      sig_hr_spike, sig_hrv_dip, sig_rr_jump, sig_stage_frag,
      sig_pressure_burst, sig_body_sd_q4, sig_combined,
      proxy_fired (debounced + occupancy-gated combined signal).
    """
    if pd is None:  # pragma: no cover
        raise ImportError("pandas required for compute_candidate_signals")
    df = per_min.sort_index().copy()

    df["sig_hr_spike"] = _rolling_pct_gate(df["hr"], ROLLING_WINDOW_MIN, 0.90)
    df["sig_hrv_dip"]  = _rolling_pct_gate(df["hrv"], ROLLING_WINDOW_MIN, 0.20,
                                           direction="below")
    df["sig_rr_jump"]  = _rolling_pct_gate(df["rr"], ROLLING_WINDOW_MIN, 0.90)

    awake = (df.get("sleep_stage", pd.Series("", index=df.index))
               .astype(str).str.lower().isin(["awake", "wake"]))
    df["sig_stage_frag"] = awake.rolling(f"{ROLLING_WINDOW_MIN}min",
                                         min_periods=1).sum().gt(0)

    p_var = df["pressure_pct"].rolling("5min", min_periods=2).std()
    p_thr = p_var.rolling(f"{ROLLING_WINDOW_MIN}min",
                          min_periods=6).quantile(0.90)
    df["sig_pressure_burst"] = p_var.gt(p_thr).fillna(False)

    body_sd = df["body_avg_f"].rolling("30min", min_periods=6).std()
    body_sd_thr = body_sd.rolling("4h", min_periods=20).quantile(0.75)
    df["sig_body_sd_q4"] = body_sd.gt(body_sd_thr).fillna(False)

    candidate_cols = ["sig_hr_spike", "sig_hrv_dip", "sig_rr_jump",
                      "sig_stage_frag", "sig_pressure_burst", "sig_body_sd_q4"]
    n_active = df[candidate_cols].astype(int).sum(axis=1)
    df["sig_combined"] = (
        df["sig_body_sd_q4"]
        & (n_active >= COMBINED_MIN_SIGNALS)
    )
    df["sig_combined_window"] = (
        df["sig_combined"].astype(int)
                          .rolling(f"{COMBINED_WINDOW_MIN}min", min_periods=1)
                          .max().astype(bool)
    )

    debounced = _persistent(df["sig_combined_window"])
    if REQUIRE_OCCUPIED and "occupied" in df.columns:
        debounced = debounced & df["occupied"].fillna(False).astype(bool)
    df["proxy_fired"] = debounced

    return df


# ── Corpus assembly ────────────────────────────────────────────────────

def build_label_corpus(per_min: "pd.DataFrame",
                       *, weights: LabelWeights = DEFAULT_WEIGHTS
                       ) -> "pd.DataFrame":
    """Assemble the final (label, weight, source) frame.

    `per_min` must already have:
      - the columns listed in compute_candidate_signals()
      - `is_override` boolean (true on override-event minutes; spans of
        consecutive override-tagged readings each count as one event for
        weighting purposes — caller decides if they want to dedupe).

    Returns the same frame with appended columns:
      discomfort_label, label_weight, label_source, proxy_signals.
    """
    if pd is None:  # pragma: no cover
        raise ImportError("pandas required")
    if "proxy_fired" not in per_min.columns:
        per_min = compute_candidate_signals(per_min)
    df = per_min.copy()

    occupied = df.get("occupied", pd.Series(True, index=df.index)).fillna(False)
    is_ovr   = df.get("is_override", pd.Series(False, index=df.index)).fillna(False)
    proxy    = df["proxy_fired"].fillna(False)

    cond_empty   = ~occupied if REQUIRE_OCCUPIED else pd.Series(False, index=df.index)
    cond_ovr     = (~cond_empty) & is_ovr
    cond_proxy   = (~cond_empty) & (~is_ovr) & proxy
    cond_silent  = (~cond_empty) & (~is_ovr) & (~proxy)

    df["discomfort_label"] = (cond_ovr | cond_proxy).astype(int)
    df["label_weight"] = 0.0
    df.loc[cond_ovr,    "label_weight"] = weights.override
    df.loc[cond_proxy,  "label_weight"] = weights.proxy
    df.loc[cond_silent, "label_weight"] = weights.silent
    df.loc[cond_empty,  "label_weight"] = weights.empty

    df["label_source"] = "empty"
    df.loc[cond_silent, "label_source"] = "silent"
    df.loc[cond_proxy,  "label_source"] = "proxy"
    df.loc[cond_ovr,    "label_source"] = "override"

    sigs = ["sig_hr_spike", "sig_hrv_dip", "sig_rr_jump",
            "sig_stage_frag", "sig_pressure_burst", "sig_body_sd_q4"]
    df["proxy_signals"] = df[sigs].apply(
        lambda r: ",".join(s.removeprefix("sig_") for s, v in r.items() if bool(v)),
        axis=1)

    return df


# ── Diagnostics helpers ────────────────────────────────────────────────

def precision_recall_vs_overrides(per_min: "pd.DataFrame",
                                  *, lead_window_min: tuple[int, int] = (5, 15)
                                  ) -> dict:
    """Quantify each candidate signal vs override events as ground truth.

    For each candidate, count it as a "hit" if it fires within the lead window
    *preceding* an override. Returns a dict[signal_name -> {precision, recall,
    fpr_per_night, n_fires, n_overrides_caught}].

    `per_min` must be sorted, minute-resolution, with `is_override` boolean
    and signal columns from `compute_candidate_signals()`.
    """
    if pd is None:  # pragma: no cover
        raise ImportError("pandas required")
    df = per_min.copy()
    if "night" not in df.columns:
        df["night"] = (df.index - pd.Timedelta(hours=6)).date

    overrides = df.index[df["is_override"].fillna(False)]
    n_nights = df["night"].nunique()
    n_overrides = len(overrides)

    out = {}
    sig_cols = ["sig_hr_spike", "sig_hrv_dip", "sig_rr_jump",
                "sig_stage_frag", "sig_pressure_burst", "sig_body_sd_q4",
                "sig_combined", "proxy_fired"]
    lo, hi = lead_window_min
    for col in sig_cols:
        if col not in df.columns:
            continue
        fires = df.index[df[col].fillna(False)]
        n_fires = len(fires)
        if n_fires == 0:
            out[col] = {"precision": 0.0, "recall": 0.0,
                        "fpr_per_night": 0.0, "n_fires": 0,
                        "n_overrides_caught": 0}
            continue

        caught = 0
        true_positive_fires = 0
        for ovr_ts in overrides:
            window_lo = ovr_ts - pd.Timedelta(minutes=hi)
            window_hi = ovr_ts - pd.Timedelta(minutes=lo)
            hits = fires[(fires >= window_lo) & (fires <= window_hi)]
            if len(hits):
                caught += 1
                true_positive_fires += len(hits)

        false_pos = max(0, n_fires - true_positive_fires)
        out[col] = {
            "precision": (true_positive_fires / n_fires) if n_fires else 0.0,
            "recall":    (caught / n_overrides) if n_overrides else 0.0,
            "fpr_per_night": (false_pos / n_nights) if n_nights else 0.0,
            "n_fires": int(n_fires),
            "n_overrides_caught": int(caught),
        }
    return out


def corpus_summary(labelled: "pd.DataFrame") -> dict:
    """One-line summary numbers for the report."""
    src = labelled["label_source"].value_counts().to_dict()
    eff = float(labelled["label_weight"].sum())
    return {
        "n_minutes":        int(len(labelled)),
        "n_override":       int(src.get("override", 0)),
        "n_proxy":          int(src.get("proxy", 0)),
        "n_silent":         int(src.get("silent", 0)),
        "n_empty":          int(src.get("empty", 0)),
        "effective_sample_size": round(eff, 2),
        "positive_minutes": int((labelled["discomfort_label"] == 1).sum()),
    }


__all__ = [
    "LabelWeights", "DEFAULT_WEIGHTS",
    "ROLLING_WINDOW_MIN", "PERSISTENCE_WINDOW_MIN", "PERSISTENCE_MIN_HITS",
    "COMBINED_MIN_SIGNALS", "COMBINED_WINDOW_MIN", "REQUIRE_OCCUPIED",
    "label_minute", "compute_candidate_signals", "build_label_corpus",
    "precision_recall_vs_overrides", "corpus_summary",
]
