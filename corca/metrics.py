"""Evaluation metrics: Top-k for ranked lists, set P/R/F1 for multi-root-cause output."""


def top_k_hit(ranked: list, truth: list, k: int) -> float:
    """Single fault: 1 if the true root cause is in the first k. Multi-fault:
    fraction of true root causes found in the first k (i.e. recall@k)."""
    truth = set(truth)
    if not truth:
        return 0.0
    return len(truth & set(ranked[:k])) / len(truth)


def set_prf(pred: list, truth: list) -> tuple:
    """Precision, recall, F1 between a predicted set and the true set."""
    pred, truth = set(pred), set(truth)
    tp = len(pred & truth)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(truth) if truth else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def exact_match(pred: list, truth: list) -> float:
    return float(set(pred) == set(truth))


# Fixed-size set baselines for RQ1/RQ2: "why not just return the base method's top k?"
TOP_K_BASELINES = (1, 2, 3)  # top-1 set = the base method as-is (RQ1)


def top_k_set_scores(base_rank: list, truth: list, ks=TOP_K_BASELINES) -> dict:
    """Score the base ranking's first k services as a root-cause *set*.

    topKset_*  : fixed k, NOT told how many roots exist (the fair baseline).
    oracle_k_* : k = true number of roots (an upper bound no deployed system has).
    """
    out = {}
    for name, k in [*[(f"top{k}set", k) for k in ks], ("oracle_k", len(truth))]:
        p, r, f = set_prf(base_rank[:k], truth)
        out.update({f"{name}_P": p, f"{name}_R": r, f"{name}_F1": f,
                    f"{name}_exact": exact_match(base_rank[:k], truth)})
    return out


TOP_K_COLUMNS = [f"{n}_{m}" for n in [*[f"top{k}set" for k in TOP_K_BASELINES], "oracle_k"]
                 for m in ("P", "R", "F1", "exact")]
