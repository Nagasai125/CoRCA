"""Report figures from scripts.report output (light theme, PNG + vector PDF).

    python -m scripts.plots results/final

  fig_f1.{png,pdf}          set F1 with 95% CI per approach, by fault count (RQ1/RQ2)
  fig_dropout.{png,pdf}     CoRCA F1 vs fraction of metric streams dropped (multi-fault)
  fig_sensitivity.{png,pdf} CoRCA F1 vs tau scale, one line per max_rounds
"""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

# Validated categorical slots (dataviz palette, light); #1baf7a/#eda100 are <3:1, so
# every series also has its own marker shape and the CSVs are the table view.
SERIES = {"corca": ("CoRCA", "#2a78d6", "o"), "top1set": ("base as-is (top-1)", "#eb6834", "s"),
          "top2set": ("top-2 set", "#1baf7a", "^"), "top3set": ("top-3 set", "#eda100", "D")}
ORACLE = ("oracle-k (upper bound)", "#8a8a85", "x")
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#6b6b66", "#e6e5e0", "#fcfcfb"
MAIN = "(drop == 0) and keep_resource and graph_mode == 'observed'"

plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
                     "axes.edgecolor": GRID, "axes.labelcolor": INK, "text.color": INK,
                     "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
                     "axes.spines.top": False, "axes.spines.right": False, "font.size": 9,
                     "lines.linewidth": 2, "lines.markersize": 6})


def parse(frame, column):
    """'0.772 [0.708, 0.833]' -> mean, low, high columns."""
    parts = frame[column].str.extract(r"(-?[\d.]+) \[(-?[\d.]+), (-?[\d.]+)\]").astype(float)
    return parts[0].to_numpy(), parts[1].to_numpy(), parts[2].to_numpy()


def save(fig, out, name):
    for ext in ("png", "pdf"):
        fig.savefig(out / f"{name}.{ext}", dpi=200)
    plt.close(fig)


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def panels(keys, cols=4):
    """Small multiples, wrapped at `cols` per row; legend goes below, never over data."""
    rows = -(-len(keys) // cols)
    width = min(len(keys), cols)
    fig, axes = plt.subplots(rows, width, figsize=(3.4 * width, 2.9 * rows + .5), sharey=True, squeeze=False)
    for ax in axes.flat[len(keys):]:
        ax.set_visible(False)
    return fig, axes.flat[:len(keys)]


def legend_below(fig, axes):
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(labels), frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, .1 / fig.get_figheight() * 3, 1, 1))


def fig_f1(rq, out):
    main = rq.query(MAIN)
    keys = list(main.groupby(["source", "method"]).groups)
    fig, axes = panels(keys)
    names = [*SERIES, "oracle_k"]
    for ax, (source, method) in zip(axes, keys):
        rows = main[(main.source == source) & (main.method == method)].sort_values("n_true")
        for i, name in enumerate(names):
            label, color, marker = SERIES.get(name, ORACLE)
            mean, low, high = parse(rows, f"{name}_F1")
            x = rows.n_true.to_numpy() + (i - (len(names) - 1) / 2) * 0.12  # dodge so CIs don't overlap
            ax.errorbar(x, mean, yerr=[mean - low, high - mean], fmt=marker, color=color, ecolor=color,
                        elinewidth=1.5, capsize=0, mfc=SURFACE if name == "oracle_k" else color,
                        label=label, zorder=3)
        counts = ", ".join(f"{plural(k, 'fault')}: n={n}" for k, n in zip(rows.n_true, rows.n_incidents))
        ax.set_title(f"{method.upper()} · {source}\n{counts}", fontsize=9, loc="left")
        ticks = sorted(rows.n_true.unique())
        ax.set_xticks(ticks)
        ax.set_xlim(ticks[0] - .5, ticks[-1] + .5)  # keep the dodge compact even for one fault count
        ax.set_xlabel("true root causes per incident")
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("set F1 (95% CI)")
    legend_below(fig, axes)
    save(fig, out, "fig_f1")


def fig_dropout(rq, out):
    rows = rq.query("n_true > 1 and keep_resource and graph_mode == 'observed'")
    if rows["drop"].nunique() < 2:
        return print("skip fig_dropout: no telemetry-dropout rows")
    keys = list(rows.groupby("source").groups)
    fig, axes = panels(keys)
    for ax, source in zip(axes, keys):
        for i, method in enumerate(sorted(rows.method.unique())):
            part = rows[(rows.source == source) & (rows.method == method)].groupby("drop").first().reset_index()
            mean, low, high = parse(part, "corca_F1")
            color, marker = [("#2a78d6", "o"), ("#eb6834", "s")][i % 2]
            ax.fill_between(part["drop"], low, high, color=color, alpha=.15, linewidth=0)
            ax.plot(part["drop"], mean, marker=marker, color=color, label=f"CoRCA-{method.upper()}")
        ax.set_title(f"multi-fault incidents · {source}", fontsize=9, loc="left")
        ax.set_xlabel("fraction of metric streams dropped")
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("CoRCA set F1 (95% CI)")
    legend_below(fig, axes)
    save(fig, out, "fig_dropout")


def fig_sensitivity(sens, out):
    keys = list(sens.groupby(["source", "method", "n_true"]).groups)
    fig, axes = panels(keys)
    for ax, (source, method, n_true) in zip(axes, keys):
        part = sens[(sens.source == source) & (sens.method == method) & (sens.n_true == n_true)]
        for (rounds, group), (color, marker) in zip(part.groupby("max_rounds"),
                                                    [("#2a78d6", "o"), ("#eb6834", "s"), ("#1baf7a", "^")]):
            ax.plot(group.tau_scale, group.corca_F1, marker=marker, color=color, label=f"max {plural(rounds, 'round')}")
        ax.axvline(1, color=MUTED, linewidth=1, linestyle=":")
        ax.set_title(f"{method.upper()} · {source} · {plural(n_true, 'fault')}", fontsize=9, loc="left")
        ax.set_xlabel("tau / calibrated tau")
        ax.set_ylim(0, 1.05)
    axes[0].set_ylabel("CoRCA set F1")
    legend_below(fig, axes)
    save(fig, out, "fig_sensitivity")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("final", help="folder written by scripts.report")
    folder = Path(parser.parse_args().final)
    rq = pd.read_csv(folder / "rq_tables.csv")
    fig_f1(rq, folder)
    fig_dropout(rq, folder)
    fig_sensitivity(pd.read_csv(folder / "sensitivity.csv"), folder)
    print(f"figures written to {folder}")


if __name__ == "__main__":
    main()
