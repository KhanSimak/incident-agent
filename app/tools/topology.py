"""
tools/topology.py — service dependency graph, same NetworkX library from
your Pipe Leak Simulator, repurposed from pipe networks to service
architecture. This is what makes "the reported symptom isn't the actual
failure point" (Scenario 3 — cart-service reports the problem, but
inventory-db is the real cause) findable at all.
"""
import networkx as nx

from app.fixtures import Scenario


def get_service_dependents(scenario: Scenario, service: str) -> dict:
    """
    Returns what `service` DEPENDS ON (its callees) and what DEPENDS ON
    IT (its callers) — both directions matter for investigation:
    - callees: "if X is broken, is it because something X calls is broken?"
    - callers: "if X is broken, what else breaks as a result (blast radius)?"
    """
    graph = nx.DiGraph()
    graph.add_edges_from(scenario.service_graph)

    if service not in graph:
        return {"service": service, "depends_on": [], "depended_on_by": [], "note": "service not found in the dependency graph"}

    return {
        "service": service,
        "depends_on": list(graph.successors(service)),      # what THIS service calls
        "depended_on_by": list(graph.predecessors(service)),  # what calls THIS service
    }
