"""Online Boutique service topology and failure-propagation helpers.

Call graph (caller -> callee) follows the architecture in
GoogleCloudPlatform/microservices-demo. Verify it against the version you deploy:
edges change rarely, but it is worth a 5-minute check before week 3.

Faults propagate *against* call direction: if `currencyservice` is slow, its
callers (`checkoutservice`, `frontend`, ...) look slow too. So the services a
root cause can "explain" are the root itself plus all its transitive callers.
"""
import networkx as nx
from urllib.parse import unquote

# caller -> callees
OB_CALLS = {
    "frontend-external": ["frontend"],  # external entry point / load balancer
    "frontend": [
        "adservice", "cartservice", "checkoutservice", "currencyservice",
        "productcatalogservice", "recommendationservice", "shippingservice",
    ],
    "checkoutservice": [
        "cartservice", "currencyservice", "emailservice", "paymentservice",
        "productcatalogservice", "shippingservice",
    ],
    "recommendationservice": ["productcatalogservice"],
    "cartservice": ["redis"],
}

# Metric suffixes that describe a service's *own* resources. A downstream fault
# rarely raises a caller's CPU/memory/disk, so these stay candidates even when
# the service is otherwise "explained" (see corca.corca keep_resource=True).
RESOURCE_SUFFIXES = ("cpu", "mem", "diskio")

# RCAEval spellings that should map onto one service node
_ALIASES = {"redis-cart": "redis"}


def call_graph(calls=OB_CALLS) -> nx.DiGraph:
    """Directed call graph, edge caller -> callee."""
    g = nx.DiGraph()
    for caller, callees in calls.items():
        for callee in callees:
            g.add_edge(caller, callee)
    return g


def metric_column(service: str, kind: str, *, legacy_aliases: bool = False) -> str:
    """Encode live IDs separately from the aliases used by legacy research datasets."""
    encoded = service.replace("%", "%25").replace("_", "%5F")
    if not legacy_aliases or service.startswith("~"):
        encoded = "~" + encoded
    return encoded + "_" + kind


def raw_service_of(col: str) -> str:
    encoded = col.split("_", 1)[0]
    return unquote(encoded[1:] if encoded.startswith("~") else encoded)


def service_of(col: str) -> str:
    """Decode exact live service IDs, retaining RCAEval aliases for legacy columns."""
    if col.startswith("~"):
        return raw_service_of(col)
    service = raw_service_of(col).replace("-db", "")
    return _ALIASES.get(service, service)


def metric_kind(col: str) -> str:
    """'cartservice_latency-90' -> 'latency-90'."""
    return col.split("_", 1)[1] if "_" in col else ""


def affected_by(root: str, g: nx.DiGraph) -> set:
    """Services whose symptoms `root` can explain: itself + all transitive callers."""
    if root not in g:
        return {root}
    return {root} | nx.ancestors(g, root)
