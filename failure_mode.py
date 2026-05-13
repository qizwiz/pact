"""
FailureMode — declarative constraint objects for pact.

A FailureMode defines a class of bug: what facts trigger it, what Z3 asserts
about those facts, and what message to show when violated.

This is the plugin layer. New constraint classes are new FailureMode instances —
no code changes to the checker. The LLM authors FailureModes; Z3 verifies them.

Inspired by ~/src/z3_spelunking/formal_failure_analysis.py.
"""

from __future__ import annotations

import ast as pyast
import functools
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    from z3 import (  # noqa: F401
        BoolVal,
        Solver,
        sat,
        unsat,
        Bool,
        And,
        Or,
        Not,
        Implies,
    )

    _HAS_Z3 = True
except ImportError:
    _HAS_Z3 = False

from .encoder import check_model_create
from .extractor import CallSite, FunctionManifest, ModelManifest


@dataclass
class FailureEvidence:
    """Concrete evidence of a FailureMode violation at a specific site."""

    mode_name: str
    file: str
    line: int
    call: str
    message: str
    missing: list[str] = field(default_factory=list)
    context: str = "failure_mode"

    def __str__(self) -> str:
        return f"{self.file}:{self.line}  [{self.mode_name}]  {self.call}  — {self.message}"


# ---------------------------------------------------------------------------
# The FailureMode type
# ---------------------------------------------------------------------------


@dataclass
class FailureMode:
    """
    Declarative specification of a constraint class.

    Parameters
    ----------
    name:
        Short identifier, e.g. "required_field_missing".
    description:
        Human-readable explanation of what this catches.
    check:
        Callable(call_site, models, functions) → list[FailureEvidence].
        Pure function — no side effects. Called once per call site.
    file_check:
        Optional Callable(file_path) → list[FailureEvidence].
        File-level scan independent of call sites. Used for modes that need
        to catch patterns in files that may have no outgoing calls (e.g. a
        module that only defines functions with mutable defaults).
        Results are deduplicated with `check` results in check_codebase().
    """

    name: str
    description: str
    check: Callable[
        [CallSite, dict[str, ModelManifest], dict[str, FunctionManifest]],
        list[FailureEvidence],
    ]
    file_check: Optional[Callable[[str], list[FailureEvidence]]] = None


# ---------------------------------------------------------------------------
# Built-in FailureModes
# (these replace the hardcoded checks in encoder.py — encoder.py stays for
#  direct use; failure_mode.py is the extensible plugin layer on top)
# ---------------------------------------------------------------------------


def _z3_missing(required: list[str], provided: set[str]) -> list[str]:
    return [f for f in required if f not in provided]


# --- 1. Universal model constraint check (presence + range + choices + max_length) ---


def _check_model_constraints(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    if not call.is_create_call or not call.model_name:
        return []
    model = models.get(call.model_name)
    if not model:
        return []
    violations = check_model_create(call, model)
    return [
        FailureEvidence(
            mode_name="model_constraint",
            file=v.file,
            line=v.line,
            call=v.call,
            message="; ".join(v.missing),
            missing=v.missing,
        )
        for v in violations
    ]


REQUIRED_FIELD_MISSING = FailureMode(
    name="model_constraint",
    description=(
        "Model.objects.create() violates one or more field constraints: "
        "presence, choices, max_length, integer range."
    ),
    check=_check_model_constraints,
)


# --- 2. Optional dereference — x.attr where x may be None -----------------
# Extracted from the AST: detects `x.something` where x is assigned from a
# call that returns Optional (common Django patterns: .first(), .get_or_none(),
# dict.get(), os.environ.get()).

_OPTIONAL_SOURCES = frozenset(
    {
        "first",
        "last",
        "get_or_none",
        "filter().first",
        "get",
        "environ.get",
        "os.environ.get",
    }
)


_OPTIONAL_RETURNING = frozenset({"first", "last", "get_or_none", "one_or_none", "get"})
_SAFE_CHECKS = frozenset({"is None", "is not None", "if not", "if "})


@functools.lru_cache(maxsize=None)
def _scan_file_optional_deref(path: str) -> list[FailureEvidence]:
    """
    File-level scan: find variables assigned from .first()/.last() etc
    that are then attribute-accessed without a None guard in between.
    Uses the AST control flow visitor from ast_z3_analysis lineage.
    """
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    evidence = []

    class _Visitor(_ast.NodeVisitor):
        def __init__(self):
            # var_name -> line where it was assigned from optional source
            self.optional_vars: dict[str, int] = {}
            self.guarded: set[str] = set()

        def _enter_scope(self):
            """Save and reset optional_vars at function/class boundaries."""
            saved = (dict(self.optional_vars), set(self.guarded))
            self.optional_vars = {}
            self.guarded = set()
            return saved

        def _exit_scope(self, saved):
            self.optional_vars, self.guarded = saved

        def visit_FunctionDef(self, node):
            saved = self._enter_scope()
            self.generic_visit(node)
            self._exit_scope(saved)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            saved = self._enter_scope()
            self.generic_visit(node)
            self._exit_scope(saved)

        def visit_Assign(self, node):
            # Visit the RHS *before* updating optional_vars so that uses of
            # the variable inside its own assignment expression (e.g.
            # `x = d.get(x.split(".")[-1], x)`) are not mis-flagged.
            self.generic_visit(node)
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)
                and isinstance(node.value, _ast.Call)
                and isinstance(node.value.func, _ast.Attribute)
                and node.value.func.attr in _OPTIONAL_RETURNING
            ):
                call_args = node.value.args
                if node.value.func.attr == "get":
                    # Django ORM: raises DoesNotExist, never returns None.
                    recv = node.value.func.value
                    # Direct: Model.objects.get(...)
                    if isinstance(recv, _ast.Attribute) and recv.attr == "objects":
                        return
                    # Chained queryset: Model.objects.select_related(...).get(...),
                    # .filter().get(), .prefetch_related(...).get(), etc.
                    # dict.get() is never called on a chained method result.
                    if isinstance(recv, _ast.Call):
                        return
                    # .get(key, non-None-default) — return type is str, not Optional
                    if len(call_args) >= 2 and not (
                        isinstance(call_args[1], _ast.Constant)
                        and call_args[1].value is None
                    ):
                        return
                    # HTTP client .get(url, headers=..., timeout=...) — kwargs that
                    # dict.get() never has are a definitive indicator of an HTTP call.
                    _HTTP_KWARGS = frozenset(
                        {"headers", "params", "timeout", "verify", "auth", "json",
                         "data", "cookies", "stream", "proxies", "cert",
                         "allow_redirects", "follow_redirects"}
                    )
                    kw_names = {kw.arg for kw in node.value.keywords}
                    if kw_names & _HTTP_KWARGS:
                        return
                    # HTTP client .get(url_var) — first arg named like a URL
                    _URL_VAR_NAMES = frozenset(
                        {"url", "URL", "uri", "URI", "endpoint", "base_url",
                         "href", "path", "route"}
                    )
                    if (
                        len(call_args) >= 1
                        and isinstance(call_args[0], _ast.Name)
                        and call_args[0].id in _URL_VAR_NAMES
                    ):
                        return
                    # HTTP client .get("http(s)://...") or .get("/url/...")
                    if (
                        len(call_args) >= 1
                        and isinstance(call_args[0], _ast.Constant)
                        and isinstance(call_args[0].value, str)
                        and (
                            call_args[0].value.startswith("/")
                            or "://" in call_args[0].value
                        )
                    ):
                        return
                    # HTTP client .get(f"/{path}") or .get(f"https://...") — f-string URL
                    if len(call_args) >= 1 and isinstance(call_args[0], _ast.JoinedStr):
                        first_part = (
                            call_args[0].values[0] if call_args[0].values else None
                        )
                        if (
                            isinstance(first_part, _ast.Constant)
                            and isinstance(first_part.value, str)
                            and (
                                first_part.value.startswith("/")
                                or "://" in first_part.value
                            )
                        ):
                            return
                    # Known HTTP client receiver names: requests.get(), session.get(),
                    # self.client.get() (Django test client), async_client.get(), etc.
                    _HTTP_CLIENTS = frozenset(
                        {"requests", "httpx", "session", "_session", "client",
                         "http_client", "http_session", "req_session", "async_client",
                         "r", "s"}
                    )
                    recv = node.value.func.value
                    if isinstance(recv, _ast.Name) and recv.id in _HTTP_CLIENTS:
                        return
                    # self.client.get(), self.async_client.get() — attribute chain
                    if isinstance(recv, _ast.Attribute) and recv.attr in _HTTP_CLIENTS:
                        return
                    # Custom class .get(non_string_key) — not a dict lookup; skip.
                    # dict.get() keys are almost always string literals or string
                    # variables (name, key, attr_name, etc.). A non-string constant
                    # key (int, bool) or a variable whose name doesn't suggest a
                    # string key (e.g. ctx, id, idx, num) indicates a custom class
                    # .get() method and should not be treated as nullable.
                    if len(call_args) >= 1:
                        key_arg = call_args[0]
                        if isinstance(key_arg, _ast.Constant) and not isinstance(
                            key_arg.value, str
                        ):
                            return  # non-string constant key → custom class
                        if isinstance(key_arg, _ast.Name) and key_arg.id in (
                            "ctx",
                            "context",
                            "id",
                            "idx",
                            "num",
                            "index",
                            "i",
                            "n",
                            "node",
                            "obj",
                            "ref",
                            "ptr",
                            "handle",
                            "fd",
                        ):
                            return  # integer-semantics variable name → custom class
                self.optional_vars[node.targets[0].id] = node.lineno
                self.guarded.discard(node.targets[0].id)

        def visit_If(self, node):
            # If the test references an optional var, mark it guarded
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            for var in list(self.optional_vars):
                if var in src:
                    self.guarded.add(var)
            self.generic_visit(node)

        def visit_IfExp(self, node):
            # Ternary: body if test else orelse.
            # Variables referenced in the test are guarded within the body branch.
            # E.g. `request.user if request else None` — `request` is safe in body.
            self.visit(node.test)
            test_src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            newly_guarded: set[str] = set()
            for var in list(self.optional_vars):
                if var in test_src and var not in self.guarded:
                    self.guarded.add(var)
                    newly_guarded.add(var)
            self.visit(node.body)
            self.guarded -= newly_guarded
            self.visit(node.orelse)
            # Do NOT call generic_visit — all children handled above.

        def visit_Attribute(self, node):
            # var.something — flag if var is unguarded optional
            if (
                isinstance(node.value, _ast.Name)
                and node.value.id in self.optional_vars
                and node.value.id not in self.guarded
            ):
                var = node.value.id
                assign_line = self.optional_vars[var]
                evidence.append(
                    FailureEvidence(
                        mode_name="optional_dereference",
                        file=path,
                        line=node.lineno,
                        call=f"{var}.{node.attr}",
                        message=(
                            f"'{var}' assigned from optional source at line {assign_line} "
                            f"but used without None check"
                        ),
                    )
                )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return evidence


def _check_optional_deref(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_optional_deref(call.file)


OPTIONAL_DEREF = FailureMode(
    name="optional_dereference",
    description=(
        "Attribute access on a value that may be None (e.g. from .first(), "
        "dict.get(), os.environ.get()). Will raise AttributeError at runtime."
    ),
    check=_check_optional_deref,
)


# --- 3. Missing required function argument ---------------------------------


def _check_required_arg(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    if call.is_create_call:
        return []
    # *args or **kwargs spreads mean we cannot statically determine coverage.
    if call.has_var_args or call.has_var_kwargs:
        return []
    func = functions.get(call.callee_name)
    if not func or not func.required_args:
        return []
    # pytest fixtures are called by the framework with injected dependencies.
    # Calling a fixture name in test code invokes the fixture's return value
    # (often a factory), not the fixture function itself.
    if func.is_pytest_fixture:
        return []
    # Only non-kwonly required args can be covered by positional args.
    # Enumerate positional-only required args separately so a kwonly arg at
    # index i is never falsely marked covered because positional_count > i.
    positional_required = [a for a in func.required_args if not a.kwonly]
    # For method calls (obj.foo(args)), the receiver is implicit and not
    # counted in positional_count, but a module-level function with the same
    # name has 'self' (or equivalent first param) as an explicit required arg.
    # Add 1 so the receiver is treated as positionally covered.
    effective_positional = call.positional_count + (1 if call.is_method_call else 0)
    positional_covered = {
        arg.name
        for i, arg in enumerate(positional_required)
        if i < effective_positional
    }
    provided = call.provided_kwargs | positional_covered
    missing = _z3_missing([a.name for a in func.required_args], provided)
    if missing:
        return [
            FailureEvidence(
                mode_name="required_arg_missing",
                file=call.file,
                line=call.line,
                call=call.callee_name,
                message=f"missing required arg(s): {', '.join(missing)}",
                missing=missing,
            )
        ]
    return []


REQUIRED_ARG_MISSING = FailureMode(
    name="required_arg_missing",
    description=(
        "Function called without all required positional arguments. "
        "Will raise TypeError at runtime."
    ),
    check=_check_required_arg,
)


# --- 4. Bare except that swallows all exceptions ---------------------------
# Detects `except:` or `except Exception: pass` — silent failure patterns.


@functools.lru_cache(maxsize=None)
def _scan_file_bare_except(path: str) -> list[FailureEvidence]:
    """File-level scan for bare except: and silent except Exception: pass."""
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    evidence = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.ExceptHandler):
            continue
        if node.type is None:
            # bare `except:` — catches KeyboardInterrupt, SystemExit, everything
            evidence.append(
                FailureEvidence(
                    mode_name="bare_except",
                    file=path,
                    line=node.lineno,
                    call="except:",
                    message="bare `except:` catches all exceptions including KeyboardInterrupt",
                )
            )
        elif isinstance(node.type, _ast.Name) and node.type.id == "Exception":
            # `except Exception: pass` or `except Exception: ...` — silent swallow
            body = node.body
            is_silent = len(body) == 1 and (
                isinstance(body[0], _ast.Pass)
                or (
                    isinstance(body[0], _ast.Expr)
                    and isinstance(body[0].value, _ast.Constant)
                    and body[0].value.value is ...
                )
            )
            if is_silent:
                evidence.append(
                    FailureEvidence(
                        mode_name="bare_except",
                        file=path,
                        line=node.lineno,
                        call="except Exception: pass",
                        message="`except Exception: pass` silently swallows all errors",
                    )
                )
    return evidence


def _check_bare_except(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_bare_except(call.file)


BARE_EXCEPT = FailureMode(
    name="bare_except",
    description=(
        "Bare `except:` or `except Exception: pass` silently swallows all errors. "
        "Makes bugs invisible."
    ),
    check=_check_bare_except,
    file_check=_scan_file_bare_except,
)


# --- 5. save() without update_fields ---------------------------------------
# Django model .save() without update_fields re-writes every column,
# clobbering concurrent partial updates.

_SAFE_SAVE_RECEIVER_KINDS = frozenset({"form", "serializer", "fs", "storage", "file"})


@functools.lru_cache(maxsize=None)
def _file_imports_django(path: str) -> bool:
    """Return True if the file contains a Django import."""
    try:
        from pathlib import Path as _Path

        src = _Path(path).read_text(encoding="utf-8", errors="replace")
        return "from django" in src or "import django" in src
    except OSError:
        return False


@functools.lru_cache(maxsize=None)
def _new_object_save_lines(path: str) -> frozenset:
    """Return set of line numbers where .save() is on a freshly-constructed object.

    A name is "freshly constructed" when it was last assigned via a constructor
    call — `x = SomeModel(...)` — not fetched from the DB (.get, .first, etc).
    These are INSERT operations; update_fields does not apply.
    """
    import ast as _ast
    from pathlib import Path as _Path

    try:
        src = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(src, filename=path)
    except (OSError, SyntaxError):
        return frozenset()

    # ORM fetch call names — result is an existing DB row, not a new object
    _ORM_FETCH = frozenset({"get", "first", "last", "latest", "earliest", "create",
                             "get_or_create", "update_or_create", "bulk_create",
                             "all", "filter", "exclude", "select_related", "prefetch_related"})

    def _is_constructor_call(node: _ast.expr) -> bool:
        """True if node is a Call whose function looks like a class constructor."""
        if not isinstance(node, _ast.Call):
            return False
        func = node.func
        # SomeClass(...) — bare name starting with uppercase
        if isinstance(func, _ast.Name):
            return func.id[:1].isupper()
        # module.SomeClass(...) or self.SomeClass(...)
        if isinstance(func, _ast.Attribute):
            # Exclude ORM chained calls: Model.objects.get(), qs.filter().first()
            if func.attr in _ORM_FETCH:
                return False
            return func.attr[:1].isupper()
        return False

    new_obj_save_lines: set[int] = set()

    for func_node in _ast.walk(tree):
        if not isinstance(func_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        # Map name → True if last assignment was a constructor call
        constructed: dict[str, bool] = {}
        for stmt in _ast.walk(func_node):
            # Track assignments: x = SomeModel(...)
            if isinstance(stmt, _ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, _ast.Name):
                        constructed[target.id] = _is_constructor_call(stmt.value)
            elif isinstance(stmt, (_ast.AnnAssign,)):
                if isinstance(stmt.target, _ast.Name) and stmt.value is not None:
                    constructed[stmt.target.id] = _is_constructor_call(stmt.value)
            # Detect name.save() or self.name.save()
            elif isinstance(stmt, _ast.Expr) and isinstance(stmt.value, _ast.Call):
                call_node = stmt.value
                if isinstance(call_node.func, _ast.Attribute) and call_node.func.attr == "save":
                    recv = call_node.func.value
                    recv_name: str | None = None
                    if isinstance(recv, _ast.Name):
                        recv_name = recv.id
                    elif isinstance(recv, _ast.Attribute) and isinstance(recv.value, _ast.Name):
                        # self.something.save() — track on "something"
                        recv_name = recv.attr
                    if recv_name and constructed.get(recv_name):
                        new_obj_save_lines.add(call_node.lineno)

    return frozenset(new_obj_save_lines)


def _check_save_without_update_fields(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    if not call.callee_name.endswith(".save"):
        return []
    if "update_fields" in call.provided_kwargs:
        return []
    # Non-Django files cannot have Django model .save() calls.
    if not _file_imports_django(call.file):
        return []
    # Positional args mean this is PIL/file/custom .save(path, format, ...) not Django.
    if call.positional_count >= 1 or call.has_var_args:
        return []
    # Split on `_` and check the last component: `user_form` → `form`, `serializer` → `serializer`.
    # This correctly skips form/serializer/storage saves (intentional full saves)
    # but `profile.save()` is not misclassified as a file save
    # (the old `.endswith("file")` check had `'profile'.endswith('file')` == True).
    receiver = call.callee_name.rsplit(".", 1)[0].split(".")[-1].lower()
    if receiver.rsplit("_", 1)[-1] in _SAFE_SAVE_RECEIVER_KINDS:
        return []
    # New-object INSERT: if the receiver was assigned from a constructor (Model(...)),
    # this is an INSERT operation — update_fields is invalid here (raises ValueError).
    if call.line in _new_object_save_lines(call.file):
        return []
    return [
        FailureEvidence(
            mode_name="save_without_update_fields",
            file=call.file,
            line=call.line,
            call=call.callee_name,
            message=(
                ".save() without update_fields re-writes every column; "
                "use save(update_fields=[...]) to prevent clobbering concurrent writes"
            ),
        )
    ]


SAVE_WITHOUT_UPDATE_FIELDS = FailureMode(
    name="save_without_update_fields",
    description=(
        "Django model .save() called without update_fields. "
        "Re-writes every column, clobbering concurrent partial updates."
    ),
    check=_check_save_without_update_fields,
)


# --- 6. Mutable default argument -------------------------------------------
# def f(x=[]) — the list is shared across every call. Mutations persist silently.


@functools.lru_cache(maxsize=None)
def _scan_file_mutable_defaults(path: str) -> list[FailureEvidence]:
    """File-level scan for mutable (list/dict/set) default arguments."""
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    evidence = []
    _MUTABLE = (_ast.List, _ast.Dict, _ast.Set)

    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for default in node.args.defaults:
            if isinstance(default, _MUTABLE):
                kind = type(default).__name__.lower()
                msg = (
                    f"mutable {kind} default in '{node.name}' — "
                    "shared across all calls; use None and allocate inside the function"
                )
                evidence.append(
                    FailureEvidence(
                        mode_name="mutable_default_arg",
                        file=path,
                        line=default.lineno,
                        call=f"def {node.name}",
                        message=msg,
                        missing=[msg],
                    )
                )
        for kw_default in node.args.kw_defaults:
            if kw_default is not None and isinstance(kw_default, _MUTABLE):
                kind = type(kw_default).__name__.lower()
                msg = (
                    f"mutable {kind} keyword default in '{node.name}' — "
                    "shared across all calls; use None and allocate inside the function"
                )
                evidence.append(
                    FailureEvidence(
                        mode_name="mutable_default_arg",
                        file=path,
                        line=kw_default.lineno,
                        call=f"def {node.name}",
                        message=msg,
                        missing=[msg],
                    )
                )
    return evidence


def _check_mutable_defaults(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_mutable_defaults(call.file)


MUTABLE_DEFAULT_ARG = FailureMode(
    name="mutable_default_arg",
    description=(
        "Function defined with a mutable default argument (list, dict, or set). "
        "The same object is shared across all calls — mutations persist between invocations."
    ),
    check=_check_mutable_defaults,
    file_check=_scan_file_mutable_defaults,
)


# --- 7. Missing await -------------------------------------------------------
# async def fetch(): ...
# result = fetch()     # creates a coroutine object; never runs


@functools.lru_cache(maxsize=None)
def _scan_file_missing_await(path: str) -> list[FailureEvidence]:
    """File-level scan for calls to async functions missing `await`."""
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    # Collect async function names, partitioned by scope:
    # - module_async: bare-name calls must be awaited
    # - method_async: self.name() / cls.name() calls must be awaited
    module_async: set[str] = set()
    method_async: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AsyncFunctionDef):
            # Async generators (contain yield) return AsyncGenerator, not a coroutine.
            # Calling them without await is correct; they're consumed via `async for`.
            if any(isinstance(n, (_ast.Yield, _ast.YieldFrom)) for n in _ast.walk(node)):
                continue
            # Heuristic: if the first argument is self/cls, it's a method
            args = node.args.args
            if args and args[0].arg in ("self", "cls"):
                method_async.add(node.name)
            else:
                module_async.add(node.name)

    if not module_async and not method_async:
        return []

    # Build a child→parent map so we can check the calling context.
    parent_map: dict[int, _ast.AST] = {}
    for node in _ast.walk(tree):
        for child in _ast.iter_child_nodes(node):
            parent_map[id(child)] = node

    # Names of functions/methods that intentionally accept a coroutine object
    # without awaiting it (they schedule it themselves).
    _CORO_CONSUMERS = frozenset(
        {
            "create_task",
            "ensure_future",
            "gather",
            "wait",
            "wait_for",
            "shield",
            "run_coroutine_threadsafe",
            "StreamingResponse",
            "EventSourceResponse",
            "run_worker",       # Textual UI framework: schedules coroutine as worker
            "call_soon",        # asyncio loop scheduling
            "call_soon_threadsafe",
        }
    )

    # asyncio.run(coro()) — qualified only; bare run() is too common
    _CORO_CONSUMERS_QUALIFIED = frozenset(
        {
            ("asyncio", "run"),
            ("loop", "run_until_complete"),
        }
    )

    def _is_coro_consumer_arg(call_node: _ast.Call) -> bool:
        """Return True if call_node is passed directly to a coroutine consumer."""
        parent = parent_map.get(id(call_node))
        if parent is None:
            return False
        # Direct argument: create_task(coro()) or gather(a(), b())
        if isinstance(parent, _ast.Call):
            func = parent.func
            fname = None
            receiver = None
            if isinstance(func, _ast.Attribute):
                fname = func.attr
                if isinstance(func.value, _ast.Name):
                    receiver = func.value.id
            elif isinstance(func, _ast.Name):
                fname = func.id
            if fname in _CORO_CONSUMERS:
                return True
            # Qualified consumers: asyncio.run(), loop.run_until_complete()
            if receiver is not None and (receiver, fname) in _CORO_CONSUMERS_QUALIFIED:
                return True
        # Collected into a list/tuple that will be passed to gather et al:
        # tasks.append(coro()) or tasks = [coro(), ...]
        if isinstance(parent, (_ast.List, _ast.Tuple, _ast.Set)):
            gp = parent_map.get(id(parent))
            if isinstance(gp, _ast.Call):
                func = gp.func
                fname = (
                    func.attr
                    if isinstance(func, _ast.Attribute)
                    else func.id if isinstance(func, _ast.Name) else None
                )
                if fname in _CORO_CONSUMERS:
                    return True
        # .append(coro()) — common pattern before asyncio.gather(*tasks)
        if isinstance(parent, _ast.Call):
            if isinstance(parent.func, _ast.Attribute) and parent.func.attr == "append":
                return True
        # task = coro() — coroutine stored for later scheduling (ensure_future, gather)
        # The bug pattern is an expression statement (parent is Expr), not an assignment.
        if isinstance(parent, (_ast.Assign, _ast.AnnAssign, _ast.AugAssign)):
            return True
        if isinstance(parent, _ast.NamedExpr):
            return True
        # async for x in coro() — correct async generator consumption
        if isinstance(parent, _ast.AsyncFor):
            return True
        # [x async for x in coro()] — async comprehension
        if isinstance(parent, _ast.comprehension) and parent.is_async:
            return True
        # coro().__await__() / .__aiter__() / .__anext__() — awaitable protocol
        # implementation (e.g. def __await__(self): return self._impl().__await__())
        if isinstance(parent, _ast.Attribute) and parent.attr in (
            "__await__", "__aiter__", "__anext__"
        ):
            return True
        return False

    evidence = []

    class _Visitor(_ast.NodeVisitor):
        def __init__(self):
            self._in_await = False
            self._in_async_def = False  # True only inside async def bodies

        def visit_AsyncFunctionDef(self, node):
            old = self._in_async_def
            self._in_async_def = True
            self.generic_visit(node)
            self._in_async_def = old

        def visit_FunctionDef(self, node):
            old = self._in_async_def
            self._in_async_def = False
            self.generic_visit(node)
            self._in_async_def = old

        def visit_Await(self, node):
            old = self._in_await
            self._in_await = True
            self.generic_visit(node)
            self._in_await = old

        def visit_AsyncWith(self, node):
            # Context expressions (the part after 'async with') are async
            # context managers — calling them is correct, not a missing await.
            old = self._in_await
            self._in_await = True
            for item in node.items:
                self.visit(item.context_expr)
            self._in_await = old
            # Body is checked normally — coroutine calls inside still need await.
            for stmt in node.body:
                self.visit(stmt)

        def visit_Call(self, node):
            if not self._in_await:
                name = None
                is_method_call = False
                if isinstance(node.func, _ast.Name):
                    name = node.func.id
                elif isinstance(node.func, _ast.Attribute):
                    name = node.func.attr
                    # Only flag method calls when receiver is self/cls to
                    # avoid false positives on unrelated objects (e.g.
                    # data.get() when async def get() exists in the same class)
                    recv = node.func.value
                    if isinstance(recv, _ast.Name) and recv.id in ("self", "cls"):
                        is_method_call = True
                # module-level async calls (save_data()) are bugs in any context.
                # method calls (self.method()) are only bugs inside async def — in sync
                # methods a shared method name may deliberately call the sync version
                # (dual sync/async client pattern: both SyncClient and AsyncClient
                # define close(), request(), etc. in the same file).
                should_flag = (
                    name in module_async and not isinstance(node.func, _ast.Attribute)
                ) or (name in method_async and is_method_call and self._in_async_def)
                if name and should_flag and not _is_coro_consumer_arg(node):
                    evidence.append(
                        FailureEvidence(
                            mode_name="missing_await",
                            file=path,
                            line=node.lineno,
                            call=name,
                            message=(
                                f"coroutine '{name}' called without await — "
                                "returns a coroutine object that is immediately discarded; "
                                "the function body never runs"
                            ),
                        )
                    )
            self.generic_visit(node)

    _Visitor().visit(tree)
    return evidence


def _check_missing_await(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_missing_await(call.file)


MISSING_AWAIT = FailureMode(
    name="missing_await",
    description=(
        "Async function called without `await`. "
        "Creates a coroutine object that is silently discarded — the function never executes."
    ),
    check=_check_missing_await,
    file_check=_scan_file_missing_await,
)


# --- 8. String format argument mismatch ------------------------------------
# "{} {}".format(x)  — 2 placeholders, 1 arg → IndexError at runtime.
# Z3 verifies: placeholder_count(fmt) == positional_args OR named args match.


@functools.lru_cache(maxsize=None)
def _scan_file_format_mismatch(path: str) -> list[FailureEvidence]:
    """File-level scan for .format() calls with mismatched placeholder/arg count."""
    import ast as _ast
    import re as _re
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    evidence = []

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        if not (isinstance(node.func, _ast.Attribute) and node.func.attr == "format"):
            continue
        fmt_node = node.func.value
        if not isinstance(fmt_node, _ast.Constant) or not isinstance(
            fmt_node.value, str
        ):
            continue

        # Skip dynamic calls: *args or **kwargs splice in unknown counts
        if any(isinstance(a, _ast.Starred) for a in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue

        fmt_str = fmt_node.value
        auto_count = len(_re.findall(r"\{\}", fmt_str))
        indexed = {int(m) for m in _re.findall(r"\{(\d+)\}", fmt_str)}
        named = set(
            _re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_]\w*)*)\}", fmt_str)
        )

        positional = len(node.args)
        kw_keys = {kw.arg for kw in node.keywords if kw.arg}

        if auto_count > 0 and positional < auto_count:
            evidence.append(
                FailureEvidence(
                    mode_name="format_arg_mismatch",
                    file=path,
                    line=node.lineno,
                    call="str.format()",
                    message=(
                        f"format string has {auto_count} positional {{}} placeholder(s) "
                        f"but only {positional} argument(s) provided"
                    ),
                )
            )
        if indexed:
            max_idx = max(indexed)
            if positional <= max_idx:
                evidence.append(
                    FailureEvidence(
                        mode_name="format_arg_mismatch",
                        file=path,
                        line=node.lineno,
                        call="str.format()",
                        message=(
                            f"format string references index {{{max_idx}}} "
                            f"but only {positional} positional argument(s) provided"
                        ),
                    )
                )
        missing_names = named - kw_keys
        if missing_names:
            evidence.append(
                FailureEvidence(
                    mode_name="format_arg_mismatch",
                    file=path,
                    line=node.lineno,
                    call="str.format()",
                    message=(
                        f"format string references name(s) {sorted(missing_names)} "
                        "not provided as keyword arguments"
                    ),
                )
            )
    return evidence


def _check_format_mismatch(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_format_mismatch(call.file)


FORMAT_ARG_MISMATCH = FailureMode(
    name="format_arg_mismatch",
    description=(
        "str.format() called with wrong number of arguments. "
        "Raises IndexError (positional) or KeyError (named) at runtime."
    ),
    check=_check_format_mismatch,
    file_check=_scan_file_format_mismatch,
)


# --- 9. LLM response unguarded index access --------------------------------
# response.choices[0].message.content — IndexError when the API returns 0 choices.
# Affects OpenAI, Anthropic (content[0]), Cohere, and any choices-style response.

_LLM_RESPONSE_SOURCES = frozenset(
    {
        "create",
        "complete",
        "generate",
        "invoke",
        "chat",
        "completions",
        "messages",
    }
)
_LLM_RESPONSE_ATTRS = frozenset({"choices", "content", "outputs", "candidates"})


@functools.lru_cache(maxsize=None)
def _scan_file_llm_response_unguarded(path: str) -> list[FailureEvidence]:
    """File-level scan for unguarded [0] index on LLM response list attributes."""
    import ast as _ast
    from pathlib import Path as _Path

    try:
        source = _Path(path).read_text(encoding="utf-8", errors="replace")
        tree = _ast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    # Collect variables assigned from LLM-style calls
    llm_vars: dict[str, int] = {}  # var_name → line
    guarded: set[str] = set()

    class _Visitor(_ast.NodeVisitor):
        def visit_Assign(self, node):
            if len(node.targets) == 1 and isinstance(node.targets[0], _ast.Name):
                val = node.value
                if isinstance(val, _ast.Call):
                    func = val.func
                    attr = (
                        func.attr
                        if isinstance(func, _ast.Attribute)
                        else (func.id if isinstance(func, _ast.Name) else None)
                    )
                    if attr and attr in _LLM_RESPONSE_SOURCES:
                        llm_vars[node.targets[0].id] = node.lineno
            self.generic_visit(node)

        def visit_If(self, node):
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            for var in list(llm_vars):
                if var in src:
                    guarded.add(var)
            self.generic_visit(node)

        def visit_Subscript(self, node):
            # Detect: llm_var.choices[0] or llm_var.content[0] etc.
            if not isinstance(node.slice, _ast.Constant) or node.slice.value != 0:
                self.generic_visit(node)
                return
            obj = node.value
            if not (
                isinstance(obj, _ast.Attribute) and obj.attr in _LLM_RESPONSE_ATTRS
            ):
                self.generic_visit(node)
                return
            root = obj.value
            var_name = root.id if isinstance(root, _ast.Name) else None
            if var_name and var_name in llm_vars and var_name not in guarded:
                evidence.append(
                    FailureEvidence(
                        mode_name="llm_response_unguarded",
                        file=path,
                        line=node.lineno,
                        call=f"{var_name}.{obj.attr}[0]",
                        message=(
                            f"'{var_name}.{obj.attr}[0]' without a length/None check — "
                            "LLM APIs can return empty lists on error, content filtering, or streaming edge cases"
                        ),
                    )
                )
            self.generic_visit(node)

    evidence: list[FailureEvidence] = []
    _Visitor().visit(tree)
    return evidence


def _check_llm_response_unguarded(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_llm_response_unguarded(call.file)


LLM_RESPONSE_UNGUARDED = FailureMode(
    name="llm_response_unguarded",
    description=(
        "Unguarded index-0 access on an LLM response list (choices, content, outputs). "
        "Raises IndexError when the API returns an empty list on error or content filtering."
    ),
    check=_check_llm_response_unguarded,
    file_check=_scan_file_llm_response_unguarded,
)


# ---------------------------------------------------------------------------
# Failure mode: unvalidated_lookup_chain
#
# Detects: a value retrieved via dict.get() is used as a subscript key in a
# second, different collection without a membership check (x in other_dict).
#
# Pattern:
#   x = mapping.get(key)      # x: Optional[T]
#   if x:                     # guards None — but NOT "x in other_mapping"
#       other[x] ...          # cross-index assumption: assumes x is valid in other
#
# This fires when the same name appears as:
#   1. LHS of an assignment whose RHS is a .get() call
#   2. Used as a subscript index (other[x]) in the same or a subsequent scope
#   without an intervening `x in other` or `x not in other` check.
#
# The bug we found in pact's own refactor.py:
#   caller = site_key_to_caller.get((v.file, v.line))
#   if caller:                  # guards None, not "caller in func_by_name"
#       func_violations[caller].append(v)   # silently drops if caller absent
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _scan_file_unvalidated_lookup_chain(path: str) -> list[FailureEvidence]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            source = fh.read()
        tree = pyast.parse(source, filename=path)
    except (SyntaxError, OSError):
        return []

    results: list[FailureEvidence] = []

    class _LookupChainVisitor(pyast.NodeVisitor):
        def __init__(self):
            # var_name → line where it was assigned via .get()
            self._get_vars: dict[str, int] = {}
            # var_name → set of collections it was membership-checked against
            self._guarded: dict[str, set[str]] = {}
            # collection names known to be defaultdicts (KeyError impossible)
            self._defaultdicts: set[str] = set()

        def _visit_scope(self, node: pyast.AST) -> None:
            # Each function/class body is a fresh variable scope — save and restore
            # so that .get() assignments in function A never pollute function B.
            saved_get = dict(self._get_vars)
            saved_guarded = {k: set(v) for k, v in self._guarded.items()}
            saved_dd = set(self._defaultdicts)
            self._get_vars = {}
            self._guarded = {}
            self._defaultdicts = set()
            self.generic_visit(node)
            self._get_vars = saved_get
            self._guarded = saved_guarded
            self._defaultdicts = saved_dd

        def visit_FunctionDef(self, node: pyast.FunctionDef) -> None:
            self._visit_scope(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_ClassDef(self, node: pyast.ClassDef) -> None:
            self._visit_scope(node)

        def _classify_assign(
            self, target_id: str, value: pyast.expr, line: int
        ) -> None:
            if not isinstance(value, pyast.Call):
                return
            if not isinstance(value.func, pyast.Attribute):
                return
            if value.func.attr == "defaultdict":
                self._defaultdicts.add(target_id)
            elif value.func.attr == "get":
                self._get_vars[target_id] = line

        def visit_Assign(self, node: pyast.Assign) -> None:
            if len(node.targets) == 1 and isinstance(node.targets[0], pyast.Name):
                self._classify_assign(node.targets[0].id, node.value, node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: pyast.AnnAssign) -> None:
            # x: SomeType = collections.defaultdict(...) — annotated assignment
            if isinstance(node.target, pyast.Name) and node.value is not None:
                self._classify_assign(node.target.id, node.value, node.lineno)
            self.generic_visit(node)

        def visit_Compare(self, node: pyast.Compare) -> None:
            # x in other_dict — record the guard
            if (
                len(node.ops) == 1
                and isinstance(node.ops[0], (pyast.In, pyast.NotIn))
                and isinstance(node.left, pyast.Name)
                and node.left.id in self._get_vars
            ):
                for comparator in node.comparators:
                    if isinstance(comparator, pyast.Name):
                        self._guarded.setdefault(node.left.id, set()).add(comparator.id)
            self.generic_visit(node)

        def visit_Subscript(self, node: pyast.Subscript) -> None:
            # other[x] — check if x was from .get() and not membership-checked
            if not isinstance(node.slice, pyast.Name):
                self.generic_visit(node)
                return
            var = node.slice.id
            if var not in self._get_vars:
                self.generic_visit(node)
                return
            # Get the collection being subscripted
            if not isinstance(node.value, pyast.Name):
                self.generic_visit(node)
                return
            collection = node.value.id
            if collection in self._defaultdicts:
                # defaultdict never raises KeyError on missing keys
                self.generic_visit(node)
                return
            guarded_against = self._guarded.get(var, set())
            if collection not in guarded_against:
                results.append(
                    FailureEvidence(
                        mode_name="unvalidated_lookup_chain",
                        file=path,
                        line=node.lineno,
                        call=f"{collection}[{var}]",
                        message=(
                            f"'{var}' came from .get() (line {self._get_vars[var]}) "
                            f"but is used as a key in '{collection}' without "
                            f"'{var} in {collection}' guard — KeyError if absent"
                        ),
                    )
                )
            self.generic_visit(node)

    visitor = _LookupChainVisitor()
    visitor.visit(tree)
    return results


def _check_unvalidated_lookup_chain(
    call: CallSite,
    models: dict[str, ModelManifest],
    functions: dict[str, FunctionManifest],
) -> list[FailureEvidence]:
    return _scan_file_unvalidated_lookup_chain(call.file)


UNVALIDATED_LOOKUP_CHAIN = FailureMode(
    name="unvalidated_lookup_chain",
    description=(
        "A value from dict.get() is used as a subscript key in a second collection "
        "without a membership check. The None guard doesn't protect against the value "
        "being absent from the second index."
    ),
    check=None,  # file_check runs once per file; per-call-site check would re-run the whole scan
    file_check=_scan_file_unvalidated_lookup_chain,
)


# ---------------------------------------------------------------------------
# Registry — all active failure modes
# ---------------------------------------------------------------------------

DEFAULT_MODES: list[FailureMode] = [
    REQUIRED_FIELD_MISSING,
    REQUIRED_ARG_MISSING,
    OPTIONAL_DEREF,
    BARE_EXCEPT,
    SAVE_WITHOUT_UPDATE_FIELDS,
    MUTABLE_DEFAULT_ARG,
    MISSING_AWAIT,
    FORMAT_ARG_MISMATCH,
    LLM_RESPONSE_UNGUARDED,
    UNVALIDATED_LOOKUP_CHAIN,
]
