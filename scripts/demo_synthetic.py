"""Sanity demo on synthetic 2-fault data: does the base ranking miss the 2nd
root cause, and does CoRCA recover it?  Not a result for the report (see corca/synthetic.py).

    python scripts/demo_synthetic.py
"""
import itertools

import pandas as pd

from corca import corca, set_prf
from corca.base import services_from_metrics
from corca.synthetic import baro_like_rank, generate


ROOTS = ["adservice", "cartservice", "checkoutservice", "currencyservice", "productcatalogservice"]


def main():
    rows = []
    for (a, b), seed in itertools.product(itertools.combinations(ROOTS, 2), range(3)):
        df, t = generate([a, b], ["cpu", "delay"], seed=seed)
        base = services_from_metrics(baro_like_rank(df, t))
        res = corca(df, t, rank_fn=baro_like_rank, tau=6.0)
        rows.append({"pair": f"{a}+{b}",
                     "base_top2_recall": set_prf(base[:2], [a, b])[1],
                     "corca_recall": set_prf(res.root_causes, [a, b])[1],
                     "corca_precision": set_prf(res.root_causes, [a, b])[0]})

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)
    print(df.groupby("pair").mean().round(2))
    print("\nmean:", df[["base_top2_recall", "corca_recall", "corca_precision"]].mean().round(3).to_dict())


if __name__ == "__main__":
    main()
