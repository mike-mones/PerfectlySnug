"""
Visualize the stage-reactive sleep temperature controller (v2).
Generates a chart showing:
  - How the controller responds to sleep stages across 90-min cycles
  - The circadian offset that shifts base settings over time
  - A simulated night with realistic stage cycling

The mathematical function:
  setting(t) = base_setting(stage(t)) + circadian_offset(t)

Reference: Reference/sleep-temperature-science.md
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from pathlib import Path


def simulate_stages(total_hours=8.0):
    """
    Simulate realistic sleep stages with ~90-min cycles.
    Early cycles: more deep. Late cycles: more REM.
    """
    dt = 1  # 1-minute resolution
    total_min = int(total_hours * 60)
    stages = []
    cycle_len = 90  # minutes

    for t in range(total_min):
        cycle_num = t // cycle_len
        cycle_pos = t % cycle_len

        # Proportions shift across cycles
        # Early: deep 35%, light 45%, REM 15%, wake 5%
        # Late:  deep 10%, light 35%, REM 50%, wake 5%
        progress = min(cycle_num / 4.0, 1.0)  # 0→1 over 5 cycles
        deep_pct = 0.35 - progress * 0.25     # 35% → 10%
        rem_pct = 0.15 + progress * 0.35       # 15% → 50%
        light_pct = 1.0 - deep_pct - rem_pct - 0.05

        # Within each cycle: light → deep → light → REM → brief wake
        # Normalized positions
        p = cycle_pos / cycle_len
        if p < 0.05:
            stages.append("awake")   # Brief wake between cycles
        elif p < 0.05 + light_pct * 0.4:
            stages.append("core")    # Descending light
        elif p < 0.05 + light_pct * 0.4 + deep_pct:
            stages.append("deep")    # Deep sleep
        elif p < 0.05 + light_pct * 0.4 + deep_pct + light_pct * 0.2:
            stages.append("core")    # Ascending light
        elif p < 0.05 + light_pct * 0.4 + deep_pct + light_pct * 0.2 + rem_pct:
            stages.append("rem")     # REM
        else:
            stages.append("core")    # Remaining light

    return stages


def compute_settings(stages, total_hours=8.0, precool_hours=1.0):
    """
    Compute the controller setting for each minute using the stage-reactive model:
      setting(t) = base_setting(stage) + circadian_offset(t)
    """
    # Base settings per stage (v1 seed — θ parameters)
    base = {"deep": -9, "core": -7, "rem": -6, "awake": -7}

    total_min = len(stages)
    times = np.arange(-precool_hours * 60, total_min) / 60  # hours

    settings = []
    all_stages = []

    # Pre-cool period
    for t in np.arange(-precool_hours * 60, 0):
        settings.append(-10)
        all_stages.append("precool")

    # Sleep period
    for i, stage in enumerate(stages):
        t_hours = i / 60
        # Circadian offset: -1 early → 0 mid → +1 late
        circadian = -1 + (t_hours / total_hours) * 2
        circadian = max(-1, min(1, circadian))

        setting = base[stage] + round(circadian)
        setting = max(-10, min(-5, setting))
        settings.append(setting)
        all_stages.append(stage)

    return times, np.array(settings), all_stages


def plot_trajectory():
    total_hours = 8.0
    precool_hours = 1.0

    stages = simulate_stages(total_hours)
    times, settings, all_stages = compute_settings(stages, total_hours, precool_hours)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 9), height_ratios=[3, 1],
                                     sharex=True)
    fig.patch.set_facecolor('#1a1a2e')
    for ax in [ax1, ax2]:
        ax.set_facecolor('#16213e')

    # Stage colors
    stage_colors = {
        "precool": "#1a237e",
        "awake": "#ef5350",
        "core": "#42a5f5",
        "deep": "#1565c0",
        "rem": "#ab47bc",
    }

    # ── Top panel: Setting over time ──
    # Color each minute by stage
    for i in range(len(times) - 1):
        color = stage_colors.get(all_stages[i], '#333')
        ax1.fill_between([times[i], times[i+1]], settings[i], -10.5,
                         color=color, alpha=0.3)

    ax1.step(times, settings, where='post', color='#4ecdc4', linewidth=2,
             label='Controller Setting', zorder=5)

    # Circadian offset line
    circ_times = np.linspace(0, total_hours, 100)
    circ_offset = -1 + (circ_times / total_hours) * 2
    ax1.plot(circ_times, -7 + circ_offset, color='#ffab91', linewidth=1.5,
             linestyle=':', alpha=0.7, label='Circadian offset (applied to base -7)')

    ax1.set_ylabel('Topper Setting', fontsize=12, color='white')
    ax1.set_ylim(-10.5, -4)
    ax1.set_yticks(range(-10, -4))
    ax1.tick_params(colors='white')
    ax1.axvline(x=0, color='white', alpha=0.3, linestyle=':')
    ax1.text(0.05, -4.3, 'Bedtime', color='white', alpha=0.5, fontsize=9)

    # Cycle markers
    for c in range(1, 6):
        ax1.axvline(x=c * 1.5, color='white', alpha=0.1, linestyle='-')
        ax1.text(c * 1.5, -4.3, f'Cycle {c}', color='white', alpha=0.3,
                fontsize=8, ha='center')

    ax1.set_title(
        'Stage-Reactive Sleep Temperature Controller — v2\n'
        'setting(t) = base(stage) + circadian_offset(t)',
        fontsize=14, fontweight='bold', color='white', pad=15
    )

    # Legend
    legend_patches = [
        mpatches.Patch(color=stage_colors["deep"], alpha=0.5, label='Deep (-9 base)'),
        mpatches.Patch(color=stage_colors["core"], alpha=0.5, label='Light (-7 base)'),
        mpatches.Patch(color=stage_colors["rem"], alpha=0.5, label='REM (-6 base)'),
        mpatches.Patch(color=stage_colors["awake"], alpha=0.5, label='Awake (-7 base)'),
        mpatches.Patch(color=stage_colors["precool"], alpha=0.5, label='Pre-cool (-10)'),
    ]
    legend = ax1.legend(handles=legend_patches, loc='lower right', fontsize=9,
                       facecolor='#16213e', edgecolor='#444', labelcolor='white')

    ax1.grid(True, alpha=0.15, color='white')

    # ── Bottom panel: Sleep stages (hypnogram-style) ──
    stage_y = {"awake": 3, "rem": 2, "core": 1, "deep": 0, "precool": -1}
    stage_ys = [stage_y.get(s, -1) for s in all_stages]

    for i in range(len(times) - 1):
        color = stage_colors.get(all_stages[i], '#333')
        ax2.fill_between([times[i], times[i+1]], stage_ys[i], -0.5,
                         color=color, alpha=0.6, step='post')
    ax2.step(times, stage_ys, where='post', color='white', linewidth=0.5, alpha=0.5)

    ax2.set_yticks([0, 1, 2, 3])
    ax2.set_yticklabels(['Deep', 'Light', 'REM', 'Awake'], fontsize=9, color='white')
    ax2.set_ylim(-0.5, 3.5)
    ax2.set_xlabel('Hours from Bedtime', fontsize=12, color='white')
    ax2.tick_params(colors='white')
    ax2.grid(True, alpha=0.15, color='white')
    ax2.set_title('Sleep Stages (simulated ~90-min cycles)', fontsize=10,
                  color='white', pad=5)

    # Annotations
    ax2.annotate('More deep sleep\nin early cycles',
                xy=(1.5, 0), fontsize=8, color='#90caf9',
                ha='center', va='top', style='italic')
    ax2.annotate('More REM sleep\nin late cycles',
                xy=(6.5, 2), fontsize=8, color='#ce93d8',
                ha='center', va='top', style='italic')

    # Parameter box
    param_text = (
        "Parameters to optimize (θ):\n"
        "  θ₁ base_deep     = -9\n"
        "  θ₂ base_light    = -7\n"
        "  θ₃ base_rem      = -6\n"
        "  θ₄ base_comfort  = -7\n"
        "  θ₅ circadian_gain = 1.0\n"
        "  θ₆ prewake       = -5\n"
        "\n"
        "setting = base(stage) + ⌊circ_gain·(-1+2t/T)⌋\n"
        "Reward: deep% + REM% − wakes"
    )
    props = dict(boxstyle='round,pad=0.5', facecolor='#0a0a23', alpha=0.8, edgecolor='#444')
    ax1.text(0.02, 0.02, param_text, transform=ax1.transAxes,
            fontsize=8, verticalalignment='bottom', color='#aaa',
            fontfamily='monospace', bbox=props)

    plt.tight_layout()

    out = Path(__file__).resolve().parent.parent.parent / "Reference" / "sleep-trajectory-v2.png"
    fig.savefig(out, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    print(f"Saved to {out}")

    # Mathematical summary
    print("\n=== Stage-Reactive Controller: setting(t) = base(stage) + circadian(t) ===")
    print()
    print("  Base settings (θ₁–θ₄):")
    print("    Deep  → -9    (promote deep sleep with cooler air)")
    print("    Light → -7    (neutral comfort)")
    print("    REM   → -6    (warmer — thermoreg impaired in REM)")
    print("    Awake → -7    (comfort midpoint — reduce wake time)")
    print()
    print("  Circadian offset (θ₅ = gain):")
    print("    offset(t) = floor(gain · (-1 + 2·t/T))")
    print("    t=0h: -1  (early night, extra cooling OK)")
    print("    t=4h:  0  (mid night, neutral)")
    print("    t=8h: +1  (late night, body at nadir, less cooling)")
    print()
    print("  Overrides:")
    print("    Pre-cool (bed empty):     -10")
    print("    Pre-wake (30min before):  -5  (θ₆)")


if __name__ == "__main__":
    plot_trajectory()


if __name__ == "__main__":
    plot_trajectory()
