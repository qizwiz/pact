"""
pact graph reduction analysis.

The fragility of a codebase scales with its call graph complexity: more nodes
and edges mean more paths for bugs to propagate, more coupling to reason about,
and more things that can break.  This module identifies structural simplification
targets — not just "fix this violation" but "eliminate this node/cycle from the
graph entirely."

Three structural anti-patterns, each with a formal graph-theory name:

  SCC tangle     A strongly connected component with size > 1. Any set of
                 functions that call each other in a cycle have mutual
                 dependency — you cannot change one without potentially
                 affecting all others. The elimination move: break the cycle
                 by extracting shared state into a single dependency direction.

  Pass-through   A node with in-degree = 1 AND out-degree = 1.  It adds a hop
                 without adding logic — pure structural noise.  The elimination
                 move: inline or delete it.

  Fan-out hub    A node with out-degree above a threshold.  It is a cognitive
                 complexity maximizer: to understand it you must understand all
                 N callees.  The elimination move: split by responsibility into
                 K cohesive sub-functions.

Three actual graph transformations (``apply_full_reduction``):

  SCC contraction      Collapse every strongly-connected component into one
                       representative node, producing a DAG (the condensation).
                       Cycles disappear; the true dependency order becomes visible.

  Dead-node pruning    Remove nodes not reachable from any live entry point
                       (public functions, decorated handlers, __main__ blocks).
                       Dead code cannot carry violations to callers.

  Transitive reduction Strip every edge u→w that is already implied by a longer
                       path u→…→w.  Produces the minimum edge set that preserves
                       all reachability relationships (Hasse diagram of the DAG).

Usage
-----
    from pact.reduce import analyze_graph_reduction, apply_full_reduction
    from pact.extractor import extract_from_codebase
    from pact.encoder import Violation

    models, functions, call_sites = extract_from_codebase(root)
    candidates = analyze_graph_reduction(functions, call_sites, violations)
    result = apply_full_reduction(functions, call_sites, violations)
    print(result.summary())
"""

from __future__ import annotations

import ast
import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .extractor import CallSite, FunctionManifest, iter_python_files
from .encoder import Violation

import warnings

try:
    import networkx as nx

    _HAS_NX = True
except ImportError:
    _HAS_NX = False
    warnings.warn(
        "networkx is not installed — all structural graph findings (SCCs, "
        "blast radii, hubs, passthroughs, fitness) will be silently skipped. "
        "Install networkx to enable full analysis.",
        UserWarning,
        stacklevel=2,
    )

try:
    import scipy.sparse.linalg as _spla

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover — scipy optional
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# R.C. Martin module metrics (instability / abstractness)
# ---------------------------------------------------------------------------

# Python 3.10+ provides sys.stdlib_module_names; fall back to a minimal set
# for older interpreters.  Ce computation excludes stdlib to avoid inflating
# efferent coupling with language builtins.
_STDLIB: frozenset[str]
try:
    _STDLIB = frozenset(sys.stdlib_module_names)  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover — Python < 3.10 in test environments
    _STDLIB = frozenset(
        {
            "abc",
            "ast",
            "asyncio",
            "builtins",
            "collections",
            "contextlib",
            "copy",
            "dataclasses",
            "datetime",
            "enum",
            "functools",
            "hashlib",
            "importlib",
            "inspect",
            "io",
            "itertools",
            "json",
            "logging",
            "math",
            "operator",
            "os",
            "pathlib",
            "pickle",
            "platform",
            "pprint",
            "queue",
            "random",
            "re",
            "shutil",
            "signal",
            "socket",
            "sqlite3",
            "string",
            "struct",
            "subprocess",
            "sys",
            "tempfile",
            "threading",
            "time",
            "traceback",
            "types",
            "typing",
            "typing_extensions",
            "unittest",
            "urllib",
            "uuid",
            "warnings",
            "weakref",
        }
    )


def _scan_module_imports(path: Path) -> tuple[set[str], int, int]:
    """Parse ``path`` and return (imported_top_level_packages, class_count, abstract_class_count).

    imported_top_level_packages: set of top-level package names imported by this
        module (e.g. ``import os.path`` → ``{"os"}``; ``from collections import
        defaultdict`` → ``{"collections"}``).
    class_count: total number of class definitions in the module.
    abstract_class_count: classes whose bases include ``abc.ABC`` or ``Protocol``,
        or which contain at least one ``@abstractmethod``-decorated method.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError):
        return set(), 0, 0

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            # ``from . import foo`` — relative import, no top-level package name
            # skip (node.module is None or empty for bare relative imports)

    class_count = 0
    abstract_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        class_count += 1
        # Check bases for ABC, Protocol, or abc.ABC / typing.Protocol
        is_abstract = False
        for base in node.bases:
            base_name = ""
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name in {"ABC", "Protocol", "ABCMeta"}:
                is_abstract = True
                break
        if not is_abstract:
            # Check for any @abstractmethod decorator on methods
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in item.decorator_list:
                    dec_name = ""
                    if isinstance(dec, ast.Name):
                        dec_name = dec.id
                    elif isinstance(dec, ast.Attribute):
                        dec_name = dec.attr
                    if dec_name == "abstractmethod":
                        is_abstract = True
                        break
                if is_abstract:
                    break
        if is_abstract:
            abstract_count += 1

    return imported, class_count, abstract_count


@dataclass
class ModuleMetrics:
    """R.C. Martin package metrics for one Python module.

    Ca  Afferent coupling  — how many other project modules import this one
    Ce  Efferent coupling  — how many external (non-stdlib) packages this module imports
    I   Instability        — Ce / (Ca + Ce); 0 = stable, 1 = unstable
    A   Abstractness       — abstract_classes / total_classes; 0 = fully concrete
    D   Distance from main sequence — |I + A - 1|; 0 = ideal, >0.5 = concern

    zone
        "zone of pain"        concrete AND stable (I<0.3, A<0.3, D>0.5)
        "zone of uselessness" abstract AND unstable (I>0.7, A>0.7, D>0.5)
        "main sequence"       everything else (near the ideal diagonal)
    """

    module: str  # dot-separated module name or file path relative to root
    file: str  # absolute or project-relative file path
    ca: int  # afferent coupling (fan-in)
    ce: int  # efferent coupling (fan-out, non-stdlib)
    instability: float  # I = Ce / (Ca + Ce)
    abstractness: float  # A
    distance: float  # D = |I + A - 1|
    zone: str  # "zone of pain" | "zone of uselessness" | "main sequence"

    def summary(self) -> str:
        return (
            f"  {self.module}  I={self.instability:.2f}  A={self.abstractness:.2f}"
            f"  D={self.distance:.2f}  [{self.zone}]  (Ca={self.ca}, Ce={self.ce})"
        )

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "file": self.file,
            "ca": self.ca,
            "ce": self.ce,
            "instability": round(self.instability, 3),
            "abstractness": round(self.abstractness, 3),
            "distance": round(self.distance, 3),
            "zone": self.zone,
        }


def _zone_label(instability: float, abstractness: float, distance: float) -> str:
    if distance > 0.5:
        if instability < 0.3 and abstractness < 0.3:
            return "zone of pain"
        if instability > 0.7 and abstractness > 0.7:
            return "zone of uselessness"
    return "main sequence"


def compute_module_metrics(root: Path, top_n: int | None = None) -> list[ModuleMetrics]:
    """Compute R.C. Martin instability/abstractness metrics for every Python module under root.

    Steps:
    1. Walk every ``.py`` file under ``root`` (using ``iter_python_files`` to honour
       the same skip-list as the rest of pact).
    2. For each file parse its import statements to collect the set of top-level
       packages it imports.  Relative imports (``from . import …``) are treated
       as intra-project edges.
    3. Build a project-level import graph: edges are ``module → imported_module``
       where the imported module is **also** a project module (relative by path).
    4. Compute Ca (in-degree), Ce (external non-stdlib out-degree), I, A, D.
    5. Return the list sorted by D descending (worst first), optionally truncated
       to ``top_n``.
    """
    root = Path(root).resolve()
    py_files = list(iter_python_files(root))
    if not py_files:
        return []

    # Build module-name → Path mapping.  Use the path relative to root as the
    # module key (e.g. ``pact/reduce.py`` → ``pact.reduce``).
    path_to_mod: dict[Path, str] = {}
    for p in py_files:
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        mod_name = ".".join(rel.with_suffix("").parts)
        path_to_mod[p] = mod_name

    mod_to_path: dict[str, Path] = {v: k for k, v in path_to_mod.items()}

    # Parse each file: collect imported packages, class counts
    file_data: dict[Path, tuple[set[str], int, int]] = {}
    for p in py_files:
        file_data[p] = _scan_module_imports(p)

    # Build the intra-project import graph to compute Ca (afferent coupling).
    # Also track Ce (external non-stdlib packages) per module.
    # Ca[mod] = number of *project* modules that import mod
    ca_count: dict[str, int] = collections.Counter()
    ce_count: dict[str, int] = {}

    for p in py_files:
        mod = path_to_mod[p]
        imported_pkgs, _, _ = file_data[p]

        # Ce: count external (non-stdlib, non-project) top-level packages
        project_top = {m.split(".")[0] for m in path_to_mod.values()}
        external_pkgs = {
            pkg
            for pkg in imported_pkgs
            if pkg not in _STDLIB and pkg not in project_top and pkg
        }
        ce_count[mod] = len(external_pkgs)

        # Ca contribution: for each imported package that matches a project module
        # (exact top-level name), credit that project module's Ca.
        for pkg in imported_pkgs:
            # Try exact match as a module name (e.g. ``pact.reduce``)
            # and as a top-level name (e.g. ``pact`` imported by something outside)
            if pkg in mod_to_path:
                if mod_to_path[pkg] != p:  # don't self-count
                    ca_count[pkg] += 1
            else:
                # Top-level package match: if ``pact`` is imported and we have
                # modules ``pact.reduce``, ``pact.checker``, credit all of them
                # would be over-counting; instead credit the __init__ if present.
                init_mod = f"{pkg}.__init__"
                if init_mod in mod_to_path and mod_to_path[init_mod] != p:
                    ca_count[init_mod] += 1

    results: list[ModuleMetrics] = []
    for p in py_files:
        mod = path_to_mod[p]
        _, class_count, abstract_count = file_data[p]

        ca = ca_count.get(mod, 0)
        ce = ce_count.get(mod, 0)
        total = ca + ce
        instability = ce / total if total > 0 else 0.0
        abstractness = abstract_count / class_count if class_count > 0 else 0.0
        distance = abs(instability + abstractness - 1.0)
        zone = _zone_label(instability, abstractness, distance)

        try:
            rel_file = str(p.relative_to(root))
        except ValueError:
            rel_file = str(p)

        results.append(
            ModuleMetrics(
                module=mod,
                file=rel_file,
                ca=ca,
                ce=ce,
                instability=instability,
                abstractness=abstractness,
                distance=distance,
                zone=zone,
            )
        )

    results.sort(key=lambda m: -m.distance)
    if top_n is not None:
        results = results[:top_n]
    return results


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ReductionCandidate:
    """One structural simplification target."""

    kind: str  # "tangle" | "passthrough" | "hub"
    primary: str  # main function name (or representative for tangle)
    members: list[str]  # all function names involved
    file: str
    line: int
    reduction_potential: int  # estimated nodes+edges eliminated by simplification
    violation_count: int  # total violations across all members
    detail: str  # human-readable explanation

    @property
    def score(self) -> float:
        """Structural complexity eliminated. Independent of violations.

        A pass-through with zero violations is equally worth removing as one
        with violations — the structural noise exists regardless of whether a
        bug has been observed at that node.  Violations are reported separately
        as ``urgency`` but do not affect rank.
        """
        return float(self.reduction_potential)

    @property
    def urgency(self) -> float:
        """Violation count at this node — annotates but does not drive rank."""
        return self.violation_count * 0.5

    def summary(self) -> str:
        kind_label = {
            "tangle": "TANGLE",
            "passthrough": "PASSTHROUGH",
            "hub": "HUB",
        }[self.kind]
        urgency_str = (
            f"  violations={self.violation_count}" if self.violation_count else ""
        )
        lines = [
            f"  {kind_label}  {self.primary}  [{self.file}:{self.line}]",
            f"    {self.detail}",
            f"    reduction_potential={self.reduction_potential}  score={self.score:.1f}{urgency_str}",
        ]
        if self.kind == "tangle" and len(self.members) > 1:
            cycle_str = " → ".join(self.members[:4])
            if len(self.members) > 4:
                cycle_str += f" → … ({len(self.members)} total)"
            lines.insert(1, f"    cycle: {cycle_str}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------


def _build_digraph(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
):
    """Return (G, func_by_name) or (None, {}) if networkx unavailable."""
    if not _HAS_NX:
        return None, {}

    G = nx.DiGraph()
    func_by_name: dict[str, FunctionManifest] = {}
    short_to_qual: dict[str, str] = {}

    for f in functions:
        G.add_node(f.name, manifest=f)
        func_by_name[f.name] = f
        short = f.name.split(".")[-1]
        short_to_qual.setdefault(short, f.name)

    func_names = set(func_by_name)
    for cs in call_sites:
        src = cs.caller_name or "__root__"
        tgt = cs.callee_name
        # Only resolve short names when the name is unique across the whole extracted
        # scope — if there are multiple functions with the same short name (e.g.
        # from_template on different classes), the fallback would collapse them into
        # one node and manufacture phantom cycles.
        if tgt not in func_names:
            short = tgt.split(".")[-1]
            qual = short_to_qual.get(short)
            # Only resolve if this short name is unambiguous (exactly one definition)
            count = sum(1 for f in func_by_name if f.split(".")[-1] == short)
            if qual and count == 1:
                tgt = qual
        G.add_edge(src, tgt)

    return G, func_by_name


def _violations_by_func(violations: list[Violation]) -> dict[str, list[Violation]]:
    by_func: dict[str, list[Violation]] = collections.defaultdict(list)
    for v in violations:
        # violations have a .call attribute (the callee); try to attribute to caller
        # by file+line attribution — best-effort
        by_func[v.file].append(v)
    return by_func


def _viols_for_member(
    member: str, func_by_name, violations: list[Violation]
) -> list[Violation]:
    """Return violations directly inside `member`'s function scope.

    Uses line-range attribution: violations whose line is >= the function's
    start line and before the next function in the same file are attributed
    to this function.  Falls back to file-level attribution when ordering
    cannot be determined.
    """
    f = func_by_name.get(member)
    if f is None:
        return []

    # Find the start of the *next* function in the same file to bound the range.
    file_funcs = sorted(
        [fm for fm in func_by_name.values() if fm.file == f.file],
        key=lambda fm: fm.line,
    )
    my_idx = next((i for i, fm in enumerate(file_funcs) if fm.name == member), None)
    if my_idx is not None and my_idx + 1 < len(file_funcs):
        next_line = file_funcs[my_idx + 1].line
        return [
            v for v in violations if v.file == f.file and f.line <= v.line < next_line
        ]
    # Last function in file — attribute anything after its start line
    return [v for v in violations if v.file == f.file and v.line >= f.line]


def _func_for_violation(v: Violation, func_by_name: dict) -> str | None:
    """Return the innermost function name that contains violation v.

    Inverse of _viols_for_member: given a (file, line) violation, find the
    enclosing function node in the call graph by scanning sorted start lines
    and returning the last function whose start line is <= violation line.
    Returns None if no function in the same file precedes the violation.
    """
    file_funcs = sorted(
        [f for f in func_by_name.values() if f.file == v.file],
        key=lambda f: f.line,
    )
    best: str | None = None
    for f in file_funcs:
        if f.line <= v.line:
            best = f.name
        else:
            break
    return best


@dataclass
class ViolationWithBlast:
    """A violation annotated with its call-graph blast radius.

    blast_radius is the number of distinct functions that can transitively
    reach the function containing this violation.  It is a verifiable,
    graph-theoretic upper bound on how many callers are exposed to the
    violation — not a heuristic severity label.

    Two violations of the same type are not equivalent: a bare_except
    reachable from 40 callers matters more than one reachable from 2.
    This is the causal metric that makes prioritization non-arbitrary.
    """

    violation: Violation
    blast_radius: int  # len(nx.ancestors(G, enclosing_func))
    enclosing_func: str  # function node name in the call graph
    reachable_from: frozenset[str]  # the ancestor set itself
    betweenness: float = 0.0  # normalized betweenness centrality of enclosing_func
    is_cut_vertex: bool = False  # True if removing this node disconnects the call graph

    def summary(self, show_callers: int = 3) -> str:
        v = self.violation
        bar_width = 10
        # Scale bar relative to blast_radius: log scale, cap at bar_width
        import math

        filled = min(bar_width, round(math.log2(self.blast_radius + 1)))
        bar = "█" * filled + "░" * (bar_width - filled)
        btw_str = f"  btw={self.betweenness:.3f}" if self.betweenness > 0 else ""
        cut_str = "  [CUT VERTEX]" if self.is_cut_vertex else ""
        lines = [
            f"  {v.file}:{v.line}  [{v.context}]  blast={self.blast_radius} [{bar}]{btw_str}{cut_str}",
            f"    {v.call}  —  {', '.join(v.missing)}",
        ]
        if show_callers and self.reachable_from:
            sample = sorted(self.reachable_from)[:show_callers]
            suffix = (
                f" … (+{self.blast_radius - show_callers} more)"
                if self.blast_radius > show_callers
                else ""
            )
            lines.append(f"    reachable from: {', '.join(sample)}{suffix}")
        return "\n".join(lines)


def compute_blast_radii(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
) -> list["ViolationWithBlast"]:
    """Annotate each violation with its call-graph blast radius.

    For each violation, finds the enclosing function in the call graph, then
    computes nx.ancestors() — the set of all functions that can transitively
    reach it.  Returns violations sorted descending by blast radius so the
    highest-impact findings appear first.

    Violations whose enclosing function cannot be located in the call graph
    (e.g., module-level code, generated code) are included with blast_radius=0
    so no finding is silently dropped.
    """
    if not _HAS_NX:
        return [
            ViolationWithBlast(
                violation=v,
                blast_radius=0,
                enclosing_func="",
                reachable_from=frozenset(),
            )
            for v in violations
        ]

    G, func_by_name = _build_digraph(functions, call_sites)
    if G is None:
        return [
            ViolationWithBlast(
                violation=v,
                blast_radius=0,
                enclosing_func="",
                reachable_from=frozenset(),
            )
            for v in violations
        ]

    # Betweenness on undirected projection: captures structural chokepoints
    # regardless of call direction.  Normalized so values are in [0, 1].
    G_undirected = G.to_undirected()
    btw: dict[str, float] = nx.betweenness_centrality(G_undirected, normalized=True)
    # Articulation points: nodes whose removal disconnects the undirected graph.
    # Exact (not approximate) — betweenness can miss them when paths are long.
    cut_vertices: set[str] = set(nx.articulation_points(G_undirected))

    results: list[ViolationWithBlast] = []
    for v in violations:
        func = _func_for_violation(v, func_by_name)
        if func and func in G:
            ancestors = nx.ancestors(G, func)
            results.append(
                ViolationWithBlast(
                    violation=v,
                    blast_radius=len(ancestors),
                    enclosing_func=func,
                    reachable_from=frozenset(ancestors),
                    betweenness=btw.get(func, 0.0),
                    is_cut_vertex=func in cut_vertices,
                )
            )
        else:
            results.append(
                ViolationWithBlast(
                    violation=v,
                    blast_radius=0,
                    enclosing_func=func or "",
                    reachable_from=frozenset(),
                    betweenness=btw.get(func or "", 0.0),
                    is_cut_vertex=(func or "") in cut_vertices,
                )
            )

    return sorted(results, key=lambda r: -r.blast_radius)


def find_bridge_violations(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
    *,
    threshold: float = 0.1,
) -> list["ViolationWithBlast"]:
    """Return violations in high-betweenness functions, sorted by betweenness desc.

    A function with high betweenness centrality lies on many shortest paths
    in the call graph — it is a structural bridge.  A violation there is more
    critical than the same violation in a leaf: fixing it unblocks the most
    transitive call chains.

    ``threshold`` is the minimum normalized betweenness (0–1) to qualify.
    Default 0.1 selects the top-decile of the graph's structural bridges.

    Cut vertices (articulation points) are sorted first regardless of betweenness
    — they are provably graph-disconnecting and always the highest priority.
    """
    ranked = compute_blast_radii(functions, call_sites, violations)
    bridges = [r for r in ranked if r.betweenness >= threshold or r.is_cut_vertex]
    return sorted(bridges, key=lambda r: (-int(r.is_cut_vertex), -r.betweenness))


def cut_vertex_files(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    coupling_edges: list | None = None,
) -> dict[str, list[str]]:
    """Return a mapping of file path → list of cut-vertex function names.

    Cut vertices (articulation points) are functions whose removal would
    disconnect the call graph — they are structurally load-bearing regardless
    of whether they have any violations. This is the NetworkX signal that
    should trigger intent analysis and Z3 contract verification.

    If coupling_edges (list of TemporalCoupling from enrich) are provided,
    co-commit pairs are overlaid as file-level nodes so that coordination
    points visible only in git history are also surfaced as cut vertices.
    """
    if not _HAS_NX:
        return {}
    G, func_by_name = _build_digraph(functions, call_sites)
    if G is None:
        return {}
    G_undirected = G.to_undirected()

    # Overlay temporal coupling edges: files that always move together are
    # structurally coupled even when the call graph doesn't show it.
    if coupling_edges:
        for tc in coupling_edges:
            a, b = tc.file_a, tc.file_b
            if a not in G_undirected:
                G_undirected.add_node(a, kind="file")
            if b not in G_undirected:
                G_undirected.add_node(b, kind="file")
            if not G_undirected.has_edge(a, b):
                G_undirected.add_edge(a, b, rel="COUPLED_WITH", strength=tc.strength)

    cut_verts: set[str] = set(nx.articulation_points(G_undirected))
    result: dict[str, list[str]] = {}
    for name in cut_verts:
        f = func_by_name.get(name)
        if f and f.file:
            result.setdefault(f.file, []).append(name)
        elif (
            name in G_undirected.nodes
            and G_undirected.nodes[name].get("kind") == "file"
        ):
            # Coupling-only node: the file itself is the cut vertex
            result.setdefault(name, []).append(name)
    return result


def find_sccs(
    G, func_by_name: dict, violations: list[Violation]
) -> list[ReductionCandidate]:
    """Find strongly connected components (call cycles) with size > 1."""
    if G is None:
        return []
    candidates = []
    for scc in nx.strongly_connected_components(G):
        if len(scc) <= 1:
            continue
        members = sorted(scc)
        # Find a representative: node with most violations or highest in-degree
        viols = [
            v for m in members for v in _viols_for_member(m, func_by_name, violations)
        ]
        rep = members[0]
        f = func_by_name.get(rep)
        file_ = f.file if f else ""
        line_ = f.line if f else 0
        # reduction_potential: breaking an SCC of size N eliminates O(N) back-edges
        # and makes the subgraph a DAG — estimate: N-1 edges eliminated
        reduction_potential = len(members) - 1
        candidates.append(
            ReductionCandidate(
                kind="tangle",
                primary=rep,
                members=members,
                file=file_,
                line=line_,
                reduction_potential=reduction_potential,
                violation_count=len(viols),
                detail=(
                    f"{len(members)} functions in a mutual call cycle — "
                    f"breaking the cycle removes {reduction_potential} back-edge(s) "
                    f"and makes the subgraph a DAG"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def find_passthroughs(
    G, func_by_name: dict, violations: list[Violation]
) -> list[ReductionCandidate]:
    """Find nodes with in-degree=1 and out-degree=1 and no violations of their own."""
    if G is None:
        return []
    candidates = []
    for node in G.nodes():
        f = func_by_name.get(node)
        if f is None:
            continue
        in_deg = G.in_degree(node)
        out_deg = G.out_degree(node)
        if in_deg != 1 or out_deg != 1:
            continue
        node_viols = _viols_for_member(node, func_by_name, violations)
        # Pass-throughs without violations are pure structural noise
        candidates.append(
            ReductionCandidate(
                kind="passthrough",
                primary=node,
                members=[node],
                file=f.file,
                line=f.line,
                # Eliminating removes 1 node + 2 edges = 3 graph elements
                reduction_potential=3,
                violation_count=len(node_viols),
                detail=(
                    f"in={in_deg} caller  out={out_deg} callee — "
                    f"pure hop with no logic of its own; inline to collapse 1 node + 2 edges"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def find_hubs(
    G,
    func_by_name: dict,
    violations: list[Violation],
    threshold: int = 8,
) -> list[ReductionCandidate]:
    """Find nodes whose out-degree exceeds threshold (cognitive complexity hubs)."""
    if G is None:
        return []
    candidates = []
    for node in G.nodes():
        f = func_by_name.get(node)
        if f is None:
            continue
        out_deg = G.out_degree(node)
        if out_deg < threshold:
            continue
        node_viols = _viols_for_member(node, func_by_name, violations)
        # Splitting into K groups of ≤4 reduces fan-out from N to ≤4
        k = (out_deg + 3) // 4  # ceil(out_deg / 4) groups
        reduction_potential = out_deg - k * 4  # edges pruned from the hub node
        candidates.append(
            ReductionCandidate(
                kind="hub",
                primary=node,
                members=[node],
                file=f.file,
                line=f.line,
                reduction_potential=max(0, reduction_potential),
                violation_count=len(node_viols),
                detail=(
                    f"fan-out={out_deg} (calls {out_deg} functions) — "
                    f"split by responsibility into {k} cohesive group(s) "
                    f"to reduce fan-out to ≤4 per group"
                ),
            )
        )
    return sorted(candidates, key=lambda c: -c.score)


def analyze_graph_reduction(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
    hub_threshold: int = 8,
) -> list[ReductionCandidate]:
    """
    Return all graph reduction candidates sorted by score (reduction_potential + violation urgency).

    The returned list interleaves tangles, pass-throughs, and hubs, ranked by
    their combined score so the highest-value structural simplifications appear first.
    """
    G, func_by_name = _build_digraph(functions, call_sites)
    if G is None:
        return []

    tangles = find_sccs(G, func_by_name, violations)
    passthroughs = find_passthroughs(G, func_by_name, violations)
    hubs = find_hubs(G, func_by_name, violations, threshold=hub_threshold)

    all_candidates = tangles + passthroughs + hubs
    return sorted(all_candidates, key=lambda c: -c.score)


# ---------------------------------------------------------------------------
# Actual graph transformations
# ---------------------------------------------------------------------------


def _live_roots(G) -> set[str]:
    """Nodes with no predecessors, or named like known entry points.

    Public functions are NOT assumed live — that masked orphaned production
    functions like compute_module_metrics that have test callers but no path
    from any CLI entry point.
    """
    roots: set[str] = set()
    for node in G.nodes():
        if G.in_degree(node) == 0:
            roots.add(node)
        # Decorated entry points: Flask/FastAPI/Celery/Django handlers often have
        # no graph-visible caller because the framework invokes them by name.
        name = node.split(".")[-1]
        if name in {
            "main",
            "__main__",
            "__init__",
            "run",
            "start",
            "setup",
            "execute",
            "handle",
            "dispatch",
            "process",
        }:
            roots.add(node)
    return roots or set(G.nodes())  # degenerate: treat all as live if none found


@dataclass
class ReductionResult:
    """Statistics and reduced graph from apply_full_reduction."""

    original_nodes: int
    original_edges: int
    after_scc_nodes: int
    after_scc_edges: int
    after_dead_nodes: int
    after_dead_edges: int
    final_nodes: int
    final_edges: int
    # Mapping: condensation node → frozenset of original node names (size>1 = was SCC)
    scc_map: dict[str, frozenset[str]]
    # Nodes removed as dead (unreachable from any live root)
    dead_nodes: frozenset[str]
    # The fully reduced DiGraph (node labels are original or SCC representative names)
    graph: object  # nx.DiGraph | None

    def summary(self) -> str:
        lines = [
            "  Graph reduction pipeline",
            f"    original:           {self.original_nodes} nodes, {self.original_edges} edges",
        ]
        scc_collapsed = self.original_nodes - self.after_scc_nodes
        dead_pruned = self.after_scc_nodes - self.after_dead_nodes
        tr_edges = self.after_dead_edges - self.final_edges
        if scc_collapsed:
            lines.append(
                f"    after SCC contract: {self.after_scc_nodes} nodes "
                f"({scc_collapsed} cycle(s) collapsed), {self.after_scc_edges} edges"
            )
        if dead_pruned:
            lines.append(
                f"    after dead-prune:   {self.after_dead_nodes} nodes "
                f"({dead_pruned} unreachable removed), {self.after_dead_edges} edges"
            )
        if tr_edges:
            lines.append(
                f"    after trans-reduce: {self.final_nodes} nodes, "
                f"{self.final_edges} edges ({tr_edges} redundant edge(s) removed)"
            )
        total_node = self.original_nodes - self.final_nodes
        total_edge = self.original_edges - self.final_edges
        lines.append(
            f"    TOTAL eliminated:   {total_node} node(s), {total_edge} edge(s) "
            f"→ {self.final_nodes} nodes / {self.final_edges} edges remain"
        )
        if self.dead_nodes:
            sample = sorted(self.dead_nodes)[:5]
            suffix = (
                f" … (+{len(self.dead_nodes)-5} more)"
                if len(self.dead_nodes) > 5
                else ""
            )
            lines.append(f"    dead functions:     {', '.join(sample)}{suffix}")
        tangled = {
            rep: members for rep, members in self.scc_map.items() if len(members) > 1
        }
        if tangled:
            for rep, members in sorted(tangled.items(), key=lambda x: -len(x[1]))[:3]:
                cycle = " → ".join(sorted(members)[:4])
                if len(members) > 4:
                    cycle += f" … ({len(members)} total)"
                lines.append(f"    scc [{rep}]: {cycle}")
        return "\n".join(lines)


def contract_sccs(G) -> tuple[object, dict[str, frozenset[str]]]:
    """Collapse every SCC into one representative node; return (condensation, scc_map).

    The condensation is a DAG.  ``scc_map[rep]`` is the frozenset of original node
    names that were merged into representative ``rep`` (the lexicographically first
    member of each SCC).  Single-node SCCs map to themselves.
    """
    if not _HAS_NX:
        return G, {}

    condensation = nx.condensation(G)
    scc_map: dict[str, frozenset[str]] = {}
    # nx.condensation labels nodes 0,1,2,... and stores original members in node attr
    relabel: dict[int, str] = {}
    for cnode in condensation.nodes():
        members: set[str] = condensation.nodes[cnode]["members"]
        rep = min(members)  # lexicographic representative
        relabel[cnode] = rep
        scc_map[rep] = frozenset(members)

    labeled = nx.relabel_nodes(condensation, relabel)
    return labeled, scc_map


def eliminate_dead(G, roots: set[str] | None = None) -> tuple[object, frozenset[str]]:
    """Remove nodes not reachable from any live root.

    Returns (pruned_G, dead_nodes).  ``roots`` defaults to the heuristic set
    from ``_live_roots`` when not provided.
    """
    if not _HAS_NX:
        return G, frozenset()

    live_roots = roots if roots is not None else _live_roots(G)
    reachable: set[str] = set()
    for root in live_roots:
        if root in G:
            reachable.update(nx.descendants(G, root))
            reachable.add(root)

    dead = frozenset(n for n in G.nodes() if n not in reachable)
    pruned = G.copy()
    pruned.remove_nodes_from(dead)
    return pruned, dead


def transitive_reduce(G) -> object:
    """Remove edges implied by longer paths (minimum edge set preserving reachability).

    Requires G to be a DAG; raises NetworkXError otherwise.  Apply after
    ``contract_sccs`` to guarantee acyclicity.
    """
    if not _HAS_NX:
        return G
    return nx.transitive_reduction(G)


def apply_full_reduction(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    violations: list[Violation],
    roots: set[str] | None = None,
) -> ReductionResult:
    """Run the full three-stage reduction pipeline and return statistics.

    Stages (in order):
      1. SCC contraction  → condense cycles into single nodes (produces a DAG)
      2. Dead-node pruning → remove nodes unreachable from live entry points
      3. Transitive reduction → strip edges implied by longer paths

    The pipeline is non-destructive: the original graph is never modified.
    """
    G, _ = _build_digraph(functions, call_sites)
    if G is None:
        return ReductionResult(
            original_nodes=0,
            original_edges=0,
            after_scc_nodes=0,
            after_scc_edges=0,
            after_dead_nodes=0,
            after_dead_edges=0,
            final_nodes=0,
            final_edges=0,
            scc_map={},
            dead_nodes=frozenset(),
            graph=None,
        )

    orig_n, orig_e = G.number_of_nodes(), G.number_of_edges()

    # Stage 1: SCC contraction → DAG
    G1, scc_map = contract_sccs(G)
    scc_n, scc_e = G1.number_of_nodes(), G1.number_of_edges()

    # Stage 2: dead-node elimination
    G2, dead = eliminate_dead(G1, roots)
    dead_n, dead_e = G2.number_of_nodes(), G2.number_of_edges()

    # Stage 3: transitive reduction (safe now that G2 is a DAG)
    try:
        G3 = transitive_reduce(G2)
    except Exception:
        G3 = G2  # non-DAG edge case: skip rather than crash
    final_n, final_e = G3.number_of_nodes(), G3.number_of_edges()

    return ReductionResult(
        original_nodes=orig_n,
        original_edges=orig_e,
        after_scc_nodes=scc_n,
        after_scc_edges=scc_e,
        after_dead_nodes=dead_n,
        after_dead_edges=dead_e,
        final_nodes=final_n,
        final_edges=final_e,
        scc_map=scc_map,
        dead_nodes=dead,
        graph=G3,
    )


# ---------------------------------------------------------------------------
# Fitness score
# ---------------------------------------------------------------------------


@dataclass
class GraphFitness:
    """Structural fitness of a call graph relative to its theoretical minimum.

    The minimum equivalent graph is the transitive reduction of the condensation
    DAG: the fewest nodes and edges that preserve all reachability relationships.
    The fitness score measures how close the actual graph is to that minimum.

    score=1.0  means the graph IS the minimum — no structural overhead.
    score=0.0  means every node and edge is redundant (degenerate).

    In practice, any real codebase scores between 0.5 and 0.95.
    """

    original_nodes: int
    original_edges: int
    minimum_nodes: int
    minimum_edges: int

    @property
    def node_ratio(self) -> float:
        """Fraction of nodes that are load-bearing (not eliminated by reduction)."""
        if self.original_nodes == 0:
            return 1.0
        return self.minimum_nodes / self.original_nodes

    @property
    def edge_ratio(self) -> float:
        """Fraction of edges that are non-redundant."""
        if self.original_edges == 0:
            return 1.0
        return self.minimum_edges / self.original_edges

    @property
    def score(self) -> float:
        """Geometric mean of node and edge ratios. 1.0 = optimal."""
        return (self.node_ratio * self.edge_ratio) ** 0.5

    @property
    def overhead_nodes(self) -> int:
        return self.original_nodes - self.minimum_nodes

    @property
    def overhead_edges(self) -> int:
        return self.original_edges - self.minimum_edges

    def summary(self) -> str:
        bar_width = 20
        filled = round(self.score * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        lines = [
            f"  structural fitness: {self.score:.2f}  [{bar}]",
            f"  nodes  {self.minimum_nodes}/{self.original_nodes} load-bearing"
            f"  ({self.overhead_nodes} overhead, {self.node_ratio:.0%} efficient)",
            f"  edges  {self.minimum_edges}/{self.original_edges} non-redundant"
            f"  ({self.overhead_edges} overhead, {self.edge_ratio:.0%} efficient)",
        ]
        if self.score >= 0.90:
            lines.append("  ✓ near-minimal — little structural overhead to remove")
        elif self.score >= 0.70:
            lines.append(
                f"  ⚠ {self.overhead_nodes} node(s) and {self.overhead_edges} edge(s)"
                " are structural overhead — run --reduce-apply to see the breakdown"
            )
        else:
            lines.append(
                f"  ✗ significant overhead: {self.overhead_nodes} excess node(s),"
                f" {self.overhead_edges} excess edge(s) — codebase complexity"
                " substantially exceeds its minimum equivalent structure"
            )
        return "\n".join(lines)


@dataclass
class StructuralCoverage:
    """Fraction of cut vertices (load-bearing joints) with verified behavioral contracts.

    A cut vertex without a contract is a structural blind spot: the call graph
    says this function is critical, but there is no formal specification of what
    it must do. This score is CI-trackable — teams can gate PRs on coverage >= N%.

    covered:   cut vertices with a behavioral_contract in the intent JSON
    total:     total cut vertices found by NetworkX
    dark:      cut vertices with no contract (the coverage gap)
    """

    covered: int
    total: int
    dark_files: list[
        tuple[str, list[str]]
    ]  # (file_path, [func_names]) with no contract

    @property
    def score(self) -> float:
        return self.covered / self.total if self.total > 0 else 1.0

    def summary(self) -> str:
        bar_width = 20
        filled = round(self.score * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        lines = [
            f"  structural coverage: {self.score:.0%}  [{bar}]"
            f"  ({self.covered}/{self.total} cut vertices have verified contracts)",
        ]
        if self.dark_files:
            lines.append(
                f"  {len(self.dark_files)} file(s) with unverified load-bearing functions:"
            )
            for path, funcs in self.dark_files[:5]:
                lines.append(f"    {path}: {', '.join(funcs)}")
            if len(self.dark_files) > 5:
                lines.append(f"    … and {len(self.dark_files) - 5} more")
            lines.append(
                "  → run: pact reduce --reduce --intent-trigger  to extract missing contracts"
            )
        else:
            lines.append("  ✓ all load-bearing joints have behavioral contracts")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "covered": self.covered,
            "total": self.total,
            "dark": [{"file": f, "functions": fns} for f, fns in self.dark_files],
        }


def compute_structural_coverage(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    intent_path: Path | None = None,
) -> StructuralCoverage:
    """Cross-reference NetworkX cut vertices with intent JSON to compute coverage score.

    Reads behavioral contracts from intent_path (or searches cwd for
    intent_pact_self.json). A cut vertex is "covered" when its file has a non-empty
    behavioral_contract in the intent JSON.

    This is the metric CI pipelines should gate on: if structural coverage drops
    below a threshold (e.g. 80%), new load-bearing joints have appeared without
    formal specifications.
    """
    cv_files = cut_vertex_files(functions, call_sites)
    if not cv_files:
        return StructuralCoverage(covered=0, total=0, dark_files=[])

    # Load intent JSON: explicit path → cwd fallback
    intent_data: dict = {}
    search_paths: list[Path] = []
    if intent_path:
        search_paths.append(Path(intent_path))
    search_paths += [
        Path.cwd() / "intent_pact_self.json",
        Path.cwd() / "intent.json",
    ]
    for p in search_paths:
        if p.exists():
            try:
                intent_data = json.loads(p.read_text())
                break
            except Exception as exc:
                warnings.warn(f"intent JSON parse failed: {exc}", RuntimeWarning)

    # Build path + basename → has_contract lookup
    has_contract: set[str] = set()
    for module in intent_data.get("modules", []):
        contract = (module.get("understanding") or {}).get("behavioral_contract", "")
        if contract and contract.strip():
            abs_path = module.get("path", "")
            has_contract.add(abs_path)
            has_contract.add(Path(abs_path).name)

    covered = 0
    dark: list[tuple[str, list[str]]] = []
    for file_path, func_names in sorted(cv_files.items()):
        if file_path in has_contract or Path(file_path).name in has_contract:
            covered += 1
        else:
            dark.append((file_path, func_names))

    return StructuralCoverage(
        covered=covered,
        total=len(cv_files),
        dark_files=dark,
    )


def compute_fitness(
    functions: list[FunctionManifest],
    call_sites: list[CallSite],
    roots: set[str] | None = None,
) -> GraphFitness:
    """Compute structural fitness: ratio of actual graph to its minimum equivalent.

    Runs the full reduction pipeline internally; the caller does not need to
    invoke apply_full_reduction separately.
    """
    result = apply_full_reduction(functions, call_sites, [], roots=roots)
    return GraphFitness(
        original_nodes=result.original_nodes,
        original_edges=result.original_edges,
        minimum_nodes=result.final_nodes,
        minimum_edges=result.final_edges,
    )


# ---------------------------------------------------------------------------
# Sheaf Laplacian spectral gap (Hansen & Ghrist 2019)
# ---------------------------------------------------------------------------


@dataclass
class SpectralResult:
    """Fiedler value of the normalized graph Laplacian — continuous fragility score.

    Approximates the sheaf Laplacian spectral gap (Hansen & Ghrist, 2019) using
    the normalized graph Laplacian of the weighted call graph.  The second-smallest
    eigenvalue (Fiedler value) measures how consistently the local behavioral
    contracts across call edges can be simultaneously satisfied.

    Interpretation:
      fiedler_value → 0   near-inconsistency across many interfaces; high fragility
      fiedler_value → 1   well-connected, consistent structure; low fragility
      n_components > 1    disconnected graph; no global consistency possible

    Labels:
      "fragile"      fiedler_value < 0.05
      "moderate"     0.05 ≤ fiedler_value < 0.30
      "robust"       fiedler_value ≥ 0.30
      "disconnected" n_components > 1
    """

    fiedler_value: float  # second-smallest eigenvalue of normalized Laplacian
    spectral_gap: float  # alias for fiedler_value (clearer name in reports)
    n_components: int  # number of connected components (0-eigenvalue multiplicity)
    fragility_label: str  # "robust" / "moderate" / "fragile" / "disconnected"

    def render(self) -> str:
        bar_width = 20
        filled = round(min(self.fiedler_value, 1.0) * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        return (
            f"  sheaf spectral gap: {self.fiedler_value:.4f}  [{bar}]"
            f"  [{self.fragility_label}]"
            f"  (components={self.n_components})"
        )

    def to_dict(self) -> dict:
        return {
            "fiedler_value": round(self.fiedler_value, 6),
            "spectral_gap": round(self.spectral_gap, 6),
            "n_components": self.n_components,
            "fragility_label": self.fragility_label,
        }


def _fragility_label(fiedler: float, n_components: int) -> str:
    if n_components > 1:
        return "disconnected"
    if fiedler < 0.05:
        return "fragile"
    if fiedler < 0.30:
        return "moderate"
    return "robust"


def compute_spectral_gap(G: object) -> SpectralResult | None:
    """Compute the Fiedler value of the normalized graph Laplacian of G.

    Approximates the sheaf Laplacian spectral gap for the call graph.  Converts
    the directed graph to undirected (sheaf Laplacian on directed graphs requires
    explicit stalk data we do not have; the normalized graph Laplacian is the
    correct approximation for pact's call graph).

    Returns None if neither networkx nor scipy is available.
    Returns a safe fallback SpectralResult(fiedler_value=1.0, ..., "robust") when
    the graph has fewer than 3 nodes.

    Complexity: O(k · |E|) where k=2 eigenvalues requested via ARPACK.
    """
    if not _HAS_NX or not _HAS_SCIPY:
        return None  # graceful degradation

    # Accept nx.DiGraph or nx.Graph; convert to undirected for Laplacian
    try:
        G_und = G.to_undirected()  # type: ignore[union-attr]
    except AttributeError:
        return None  # not a networkx graph

    n = G_und.number_of_nodes()
    if n < 3:
        # Too small for meaningful spectral analysis
        return SpectralResult(
            fiedler_value=1.0,
            spectral_gap=1.0,
            n_components=1,
            fragility_label="robust",
        )

    # Connected-component count: multiplicity of eigenvalue 0
    n_components = nx.number_connected_components(G_und)

    # Normalized Laplacian matrix (sparse)
    L = nx.normalized_laplacian_matrix(G_und, weight=None).astype(float)

    # Request k=2 smallest eigenvalues via ARPACK shift-invert
    # k must be < matrix dimension; guard for very small graphs
    k = min(2, n - 1)
    try:
        eigenvalues = _spla.eigsh(
            L,
            k=k,
            which="SM",
            return_eigenvectors=False,
            tol=1e-6,
        )
        eigenvalues = sorted(eigenvalues.real)
    except Exception:  # ARPACK convergence failures, singular matrices, etc.
        # Fall back: return a safe "unknown" moderate result rather than crash
        return SpectralResult(
            fiedler_value=0.0,
            spectral_gap=0.0,
            n_components=n_components,
            fragility_label=_fragility_label(0.0, n_components),
        )

    # Second-smallest eigenvalue (Fiedler value)
    # For a connected graph eigenvalues[0] ≈ 0; eigenvalues[1] is the gap.
    # For a disconnected graph both may be ≈ 0.
    fiedler = max(eigenvalues[-1], 0.0)  # clamp numerical noise below 0

    label = _fragility_label(fiedler, n_components)
    return SpectralResult(
        fiedler_value=fiedler,
        spectral_gap=fiedler,
        n_components=n_components,
        fragility_label=label,
    )
