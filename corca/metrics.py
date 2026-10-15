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
