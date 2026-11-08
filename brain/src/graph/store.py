"""In-memory property graph: typed nodes, named links, bounded memory (stdlib only).

Nodes are keyed "<kind>:<id>" so ids of different kinds never collide. Every kind except
the city (zone, sensor, actuator, unit) has a cap; past it the oldest node is dropped with
its links. Nodes are also indexed by correlation_id, which is how a chain is followed.
Not thread-safe on its own: the service holds a lock around every call.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterator

CITY_KINDS = ("zone", "sensor", "actuator", "unit")


@dataclass(slots=True)
class Node:
    kind: str
    id: str
    at: float                                   # local receive time (epoch s)
    attrs: dict[str, Any] = field(default_factory=dict)
    corr: str | None = None
    chains: list[str] = field(default_factory=list)   # correlation ids this node is listed under

    @property
    def key(self) -> str:
        return key(self.kind, self.id)


def key(kind: str, ident: str) -> str:
    return f"{kind}:{ident}"


class Graph:
    def __init__(self, caps: dict[str, int]):
        self.caps = {k: int(v) for k, v in caps.items()}
        self.nodes: dict[str, Node] = {}
        self.by_kind: dict[str, OrderedDict[str, None]] = {}
        self.out: dict[str, set[tuple[str, str]]] = {}     # key -> {(rel, dst key)}
        self.inc: dict[str, set[tuple[str, str]]] = {}     # key -> {(rel, src key)}
        self.by_corr: dict[str, list[str]] = {}
        self.dropped = 0

    # --- nodes ----------------------------------------------------------------------

    def get(self, kind: str, ident: str) -> Node | None:
        return self.nodes.get(key(kind, ident))

    def put(self, kind: str, ident: str, at: float, corr: str | None = None, **attrs: Any) -> Node:
        """Add a node, or merge attrs into the existing one (its age is kept)."""
        k = key(kind, ident)
        node = self.nodes.get(k)
        if node is not None:
            node.attrs.update(attrs)
            if corr and node.corr is None:
                node.corr = corr
                self.index_corr(node, corr)
            return node
        node = Node(kind, ident, at, dict(attrs), corr)
        self.nodes[k] = node
        self.by_kind.setdefault(kind, OrderedDict())[k] = None
        if corr:
            self.index_corr(node, corr)
        cap = self.caps.get(kind)
        if cap is not None and kind not in CITY_KINDS:
            order = self.by_kind[kind]
            while len(order) > cap:
                oldest = next(iter(order))
                self.remove(oldest)
                self.dropped += 1
        return node

    def index_corr(self, node: Node, corr: str) -> None:
        """Also list `node` in the chain of `corr` (an ack belongs to its command's chain)."""
        if corr in node.chains:
            return
        node.chains.append(corr)
        self.by_corr.setdefault(corr, []).append(node.key)

    def remove(self, k: str) -> None:
        node = self.nodes.pop(k, None)
        if node is None:
            return
        self.by_kind.get(node.kind, OrderedDict()).pop(k, None)
        for rel, dst in self.out.pop(k, set()):
            self.inc.get(dst, set()).discard((rel, k))
        for rel, src in self.inc.pop(k, set()):
            self.out.get(src, set()).discard((rel, k))
        for corr in node.chains:
            lst = self.by_corr.get(corr)
            if lst and k in lst:
                lst.remove(k)
                if not lst:
                    del self.by_corr[corr]

    def of_kind(self, kind: str) -> Iterator[Node]:
        for k in self.by_kind.get(kind, ()):
            yield self.nodes[k]

    def count(self, kind: str) -> int:
        return len(self.by_kind.get(kind, ()))

    # --- links ----------------------------------------------------------------------

    def link(self, src: Node | None, rel: str, dst: Node | None) -> None:
        if src is None or dst is None or src.key == dst.key:
            return
        self.out.setdefault(src.key, set()).add((rel, dst.key))
        self.inc.setdefault(dst.key, set()).add((rel, src.key))

    def targets(self, node: Node, rel: str | None = None, kind: str | None = None) -> list[Node]:
        out = [self.nodes[d] for r, d in self.out.get(node.key, ()) if (rel is None or r == rel) and d in self.nodes]
        return [n for n in out if kind is None or n.kind == kind]

    def sources(self, node: Node, rel: str | None = None, kind: str | None = None) -> list[Node]:
        out = [self.nodes[s] for r, s in self.inc.get(node.key, ()) if (rel is None or r == rel) and s in self.nodes]
        return [n for n in out if kind is None or n.kind == kind]

    def edges(self) -> Iterator[tuple[str, str, str]]:
        for src, links in self.out.items():
            for rel, dst in links:
                yield src, rel, dst

    def chain(self, corr: str) -> list[Node]:
        return [self.nodes[k] for k in self.by_corr.get(corr, ()) if k in self.nodes]
