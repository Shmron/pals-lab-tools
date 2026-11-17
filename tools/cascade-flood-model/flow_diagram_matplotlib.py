"""
Pipeline flow diagram, built in matplotlib (not hand-coded SVG) specifically
so it can be self-verified by reading the rendered PNG before shipping -
the SVG version had text/box overlaps that went unnoticed without a preview.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

fig, ax = plt.subplots(figsize=(20, 13), dpi=150)
ax.set_xlim(0, 20)
ax.set_ylim(0, 13)
ax.axis("off")

COL_SURFACE = "#ffffff"
COL_ACCENT_FILL = "#d8f0ec"
COL_ACCENT_EDGE = "#1f8a80"
COL_HAZARD_FILL = "#f6e2d3"
COL_HAZARD_EDGE = "#b85a26"
COL_INK = "#14201f"
COL_SOFT = "#4b5a56"


def box(x, y, w, h, lines, fill=COL_SURFACE, edge=COL_INK, dashed=False, fontsizes=None, weights=None):
    """Box centered at (x,y) with width w, height h. lines = list of text lines."""
    b = FancyBboxPatch((x - w/2, y - h/2), w, h, boxstyle="round,pad=0.04,rounding_size=0.08",
                        linewidth=1.6, edgecolor=edge, facecolor=fill,
                        linestyle="--" if dashed else "-", zorder=3)
    ax.add_patch(b)
    n = len(lines)
    if fontsizes is None:
        fontsizes = [12] + [10.5] * (n - 1)
    if weights is None:
        weights = ["bold"] + ["normal"] * (n - 1)
    line_h = h / (n + 1)
    for i, line in enumerate(lines):
        ty = y + h/2 - line_h * (i + 1)
        ax.text(x, ty, line, ha="center", va="center", fontsize=fontsizes[i],
                 fontweight=weights[i], color=COL_INK, zorder=4)
    return (x, y, w, h)


def arrow(xy1, xy2, color=COL_INK, dashed=False, lw=1.6, connectionstyle="arc3,rad=0.0"):
    a = FancyArrowPatch(xy1, xy2, arrowstyle="-|>", mutation_scale=16, linewidth=lw,
                         color=color, linestyle="--" if dashed else "-",
                         connectionstyle=connectionstyle, zorder=2)
    ax.add_patch(a)


def label(x, y, text, fontsize=9.5, color=COL_SOFT, style="normal", ha="center"):
    ax.text(x, y, text, ha=ha, va="center", fontsize=fontsize, color=color,
             style=style, zorder=4)


# ---------------------------------------------------------------------
# Formula banner
# ---------------------------------------------------------------------
banner = FancyBboxPatch((0.3, 11.3), 19.4, 1.5, boxstyle="round,pad=0.05,rounding_size=0.1",
                          linewidth=1.8, edgecolor=COL_ACCENT_EDGE, facecolor=COL_ACCENT_FILL, zorder=3)
ax.add_patch(banner)
ax.text(10, 12.45, "THE QUESTION WE ARE ANSWERING", ha="center", fontsize=13, fontweight="bold", zorder=4)
ax.text(10, 12.1, "For a flood event, how much does population-weighted travel time to the nearest health facility change?",
        ha="center", fontsize=11, zorder=4)
ax.text(10, 11.75, r"$A = \Sigma(P_i \cdot T_i) / \Sigma P_i$        $\Delta A = A_{flooded} - A_{baseline}$",
        ha="center", fontsize=12.5, fontweight="bold", zorder=4)
ax.text(10, 11.48, r"$P_i$ = population of grid cell $i$   ·   $T_i$ = travel time from $i$ to its nearest facility",
        ha="center", fontsize=9.5, color=COL_SOFT, zorder=4)

# ---------------------------------------------------------------------
# ERA5-Land: optional, parallel input (dashed)
# ---------------------------------------------------------------------
box(1.6, 10.0, 2.6, 1.1, ["ERA5-Land monthly", "(optional screening)"], dashed=True)
arrow((1.6, 9.45), (1.6, 8.75), dashed=True)
label(5.3, 9.1, "not required first — just helps prioritize which site / month to examine",
      fontsize=9, style="italic")

# ---------------------------------------------------------------------
# Mandatory pipeline start
# ---------------------------------------------------------------------
box(1.6, 8.2, 2.6, 1.1, ["Catchment-size", "classifier (MERIT upa)"])

# fork - straight lines, labels offset above the line, clear of the curve entirely
arrow((2.9, 8.5), (4.1, 10.05))
label(3.0, 9.55, "small catchment", fontsize=9, ha="left")
label(3.0, 9.32, "(< 3,000 km²)", fontsize=9, ha="left")

arrow((2.9, 7.9), (4.1, 6.35))
label(3.0, 6.85, "large catchment", fontsize=9, ha="left")
label(3.0, 6.62, "(> 3,000 km²)", fontsize=9, ha="left")

# ---------------------------------------------------------------------
# Small-catchment branch (top)
# ---------------------------------------------------------------------
box(5.5, 10.6, 2.4, 1.1, ["MERIT Hydro", "elevation + flow dir."])
arrow((6.7, 10.6), (8.5, 10.6))
label(7.6, 10.85, "HAND +", fontsize=8.7)
label(7.6, 10.35, "Leopold scaling", fontsize=8.7)

box(10.1, 10.6, 3.2, 1.3, ["Self-computed floodplain", r"HAND $\leq$ 0.01 $\times A^{0.30}$", "(A = drainage area, m²)"],
    fill=COL_ACCENT_FILL, edge=COL_ACCENT_EDGE, fontsizes=[11, 10.5, 8.5], weights=["bold", "bold", "normal"])

arrow((11.0, 9.95), (12.1, 8.55), connectionstyle="arc3,rad=-0.2", color=COL_ACCENT_EDGE)
label(12.2, 9.35, "flooded if inside", fontsize=9, color=COL_ACCENT_EDGE)
label(12.2, 9.1, "the floodplain", fontsize=9, color=COL_ACCENT_EDGE)

# ---------------------------------------------------------------------
# CHIRPS: independent, rainfall-based cross-check on the floodplain
# (does not define its extent - the floodplain above is geomorphic only)
# ---------------------------------------------------------------------
box(16.6, 10.6, 3.2, 1.3, ["CHIRPS event rainfall", "risk index = rainfall x upa", "(cross-check, not an input)"],
    dashed=True, fontsizes=[10.5, 9.5, 8.5], weights=["bold", "normal", "normal"])
arrow((15.0, 10.6), (11.7, 10.6), dashed=True, color=COL_ACCENT_EDGE)
label(13.35, 10.95, "top 15 by rainfall x area rank,", fontsize=8.5, color=COL_ACCENT_EDGE)
label(13.35, 10.75, "15/15 fall inside the floodplain", fontsize=8.5, color=COL_ACCENT_EDGE)

# ---------------------------------------------------------------------
# Large-catchment branch (bottom, mirrored)
# ---------------------------------------------------------------------
box(5.5, 5.8, 2.4, 1.1, ["JRC", "RP10 simulated depth"])
arrow((6.7, 5.8), (8.5, 5.8))
label(7.6, 6.05, "depth >", fontsize=8.7)
label(7.6, 5.55, "0.3m impassable", fontsize=8.7)

box(10.1, 5.8, 3.2, 1.3, ["Confirmed flooded", "main-river crossings", "(agrees with self-computed, 3/3)"],
    fill=COL_HAZARD_FILL, edge=COL_HAZARD_EDGE, fontsizes=[11, 10.5, 8.5], weights=["bold", "bold", "normal"])

arrow((11.0, 6.45), (12.1, 7.85), connectionstyle="arc3,rad=0.2", color=COL_HAZARD_EDGE)
label(12.5, 7.15, "independently", fontsize=9, color=COL_HAZARD_EDGE)
label(12.5, 6.9, "validated", fontsize=9, color=COL_HAZARD_EDGE)

# ---------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------
box(13.9, 8.2, 2.6, 1.2, ["Flooded crossings", "(this event)"], fill=COL_ACCENT_FILL, edge=COL_ACCENT_EDGE)

# down to band 2 - ONE elbow connector, arrowhead only at the very end
ax.plot([13.9, 13.9], [7.6, 4.3], color=COL_INK, linewidth=1.6, zorder=2)
ax.plot([13.9, 3.1], [4.3, 4.3], color=COL_INK, linewidth=1.6, zorder=2)
arrow((3.1, 4.3), (3.1, 3.15))
label(9, 4.55, "removes these edges from the routing graph", fontsize=9.5, style="italic")

# ---------------------------------------------------------------------
# Band 2: routing -> accessibility -> output
# ---------------------------------------------------------------------
box(3.1, 2.5, 2.8, 1.1, ["Routing graph", "walk 5km/h · drive by type"])

box(3.1, 0.9, 2.8, 0.9, ["OSM roads +", "bridges / fords"])
arrow((3.1, 1.35), (3.1, 1.95))
label(4.9, 1.65, "network topology", fontsize=8.7)

arrow((4.5, 2.5), (5.9, 2.5))
label(5.2, 2.85, "Dijkstra,", fontsize=8.7)
label(5.2, 2.2, "baseline vs. flooded", fontsize=8.7)

box(7.6, 2.5, 3.0, 1.1, ["Population-weighted", "accessibility (A, ΔA)"])

box(7.6, 0.9, 3.0, 0.9, ["WorldPop +", "H3 grid"])
arrow((7.6, 1.35), (7.6, 1.95))
label(9.5, 1.65, "population weights", fontsize=8.7)

arrow((9.1, 2.5), (10.5, 2.5))
label(9.8, 2.85, "per facility,", fontsize=8.7)
label(9.8, 2.2, "per grid cell", fontsize=8.7)

box(12.6, 2.5, 3.2, 1.1, ["Facility dashboards +", "consolidated record"], fill=COL_ACCENT_FILL, edge=COL_ACCENT_EDGE)

plt.tight_layout()
out_path = os.path.join(BASE_DIR, "flow_diagram.png")
plt.savefig(out_path, bbox_inches="tight", facecolor="white")
print(f"[OK] Saved -> {out_path}")
