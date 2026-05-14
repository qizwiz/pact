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


def _chain_has_objects(node) -> bool:
    """Return True if 'objects' appears anywhere in a method-call chain.

    Django ORM querysets always flow through Model.objects; pandas groupby
    chains and SQLAlchemy queries don't. Used to distinguish .first()/.last()
    that may return None (Django ORM) from ones that can't (pandas, etc.).
    """
    import ast as _ast_inner

    while True:
        if isinstance(node, _ast_inner.Attribute):
            if node.attr == "objects":
                return True
            node = node.value
        elif isinstance(node, _ast_inner.Call):
            node = node.func
        else:
            return False


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
                if node.value.func.attr in (
                    "first",
                    "last",
                    "get_or_none",
                    "one_or_none",
                ):
                    recv = node.value.func.value
                    # Django ORM .first()/.last() on plain queryset: Model.objects.first()
                    # — the only case that legitimately returns None. Everything else
                    # (pandas groupby.first(), SQLAlchemy .first(), etc.) does not.
                    if not _chain_has_objects(recv):
                        return
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
                    # Zero-arg .get() — dict.get() requires at least one positional
                    # arg (the key); a bare .get() call is a custom class method
                    # (e.g. Twisted DeferredQueue.get(), stats collector .get()).
                    if not call_args and not node.value.keywords:
                        return
                    # Django ORM queryset.get(**kwargs) — no positional args, only
                    # keyword field lookups like .get(pk=1) or .get(user=user).
                    # dict.get() ALWAYS takes a positional key; kwargs-only means ORM.
                    if not call_args and node.value.keywords:
                        return
                    # .get(key, non-None-default) — return type is str, not Optional
                    if len(call_args) >= 2 and not (
                        isinstance(call_args[1], _ast.Constant)
                        and call_args[1].value is None
                    ):
                        return
                    # .get(key, default=non-None-value) — keyword default (e.g.
                    # xml_element.get("attr", default="fallback")) is also non-Optional
                    for kw in node.value.keywords:
                        if kw.arg == "default" and not (
                            isinstance(kw.value, _ast.Constant)
                            and kw.value.value is None
                        ):
                            return
                    # HTTP client .get(url, headers=..., timeout=...) — kwargs that
                    # dict.get() never has are a definitive indicator of an HTTP call.
                    _HTTP_KWARGS = frozenset(
                        {
                            "headers",
                            "params",
                            "timeout",
                            "verify",
                            "auth",
                            "json",
                            "data",
                            "cookies",
                            "stream",
                            "proxies",
                            "cert",
                            "allow_redirects",
                            "follow_redirects",
                        }
                    )
                    kw_names = {kw.arg for kw in node.value.keywords}
                    if kw_names & _HTTP_KWARGS:
                        return
                    # HTTP client .get(url_var) — first arg named like a URL
                    _URL_VAR_NAMES = frozenset(
                        {
                            "url",
                            "URL",
                            "uri",
                            "URI",
                            "endpoint",
                            "base_url",
                            "href",
                            "path",
                            "route",
                        }
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
                    # HTTP client .get("/path?" + params) — string concat where
                    # the leftmost literal looks like a URL path or absolute URL.
                    if len(call_args) >= 1 and isinstance(call_args[0], _ast.BinOp):
                        left = call_args[0].left
                        while isinstance(left, _ast.BinOp):
                            left = left.left
                        if (
                            isinstance(left, _ast.Constant)
                            and isinstance(left.value, str)
                            and (left.value.startswith("/") or "://" in left.value)
                        ):
                            return
                        # self.url_prefix + '/workers' — rightmost component is a
                        # URL path segment even when leftmost is a variable (e.g.
                        # Tornado AsyncHTTPTestCase.get(self.url_prefix + '/path')).
                        right = call_args[0].right
                        while isinstance(right, _ast.BinOp):
                            right = right.right
                        if (
                            isinstance(right, _ast.Constant)
                            and isinstance(right.value, str)
                            and right.value.startswith("/")
                        ):
                            return
                    # Known HTTP client receiver names: requests.get(), session.get(),
                    # self.client.get() (Django test client), async_client.get(), etc.
                    _HTTP_CLIENTS = frozenset(
                        {
                            "requests",
                            "httpx",
                            "session",
                            "_session",
                            "client",
                            "http_client",
                            "http_session",
                            "req_session",
                            "async_client",
                            "r",
                            "s",
                        }
                    )
                    recv = node.value.func.value
                    if (
                        isinstance(recv, _ast.Name)
                        and recv.id.lstrip("_") in _HTTP_CLIENTS
                    ):
                        return
                    # self.client.get(), self.async_client.get(), self.__session.get()
                    # — attribute chain; strip leading underscores for private attrs.
                    if (
                        isinstance(recv, _ast.Attribute)
                        and recv.attr.lstrip("_") in _HTTP_CLIENTS
                    ):
                        return
                    # self.client.collections.get() — grandparent is a known HTTP/API
                    # client (e.g. Weaviate client.collections.get(), FastAPI test
                    # client.app.get()).  Two-level chains on a client namespace are
                    # never plain dict lookups.
                    if (
                        isinstance(recv, _ast.Attribute)
                        and isinstance(recv.value, _ast.Attribute)
                        and recv.value.attr.lstrip("_") in _HTTP_CLIENTS
                    ):
                        return
                    # client.containers.get() — Docker SDK / plain client variable
                    # at the root: recv is Attribute(value=Name('client'), attr='containers')
                    # so recv.value is a bare Name, not an Attribute.
                    if (
                        isinstance(recv, _ast.Attribute)
                        and isinstance(recv.value, _ast.Name)
                        and recv.value.id.lstrip("_") in _HTTP_CLIENTS
                    ):
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

        def visit_Assert(self, node):
            # `assert x is not None` — permanently guards x in this scope.
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            for var in list(self.optional_vars):
                if var in src:
                    self.guarded.add(var)
            self.generic_visit(node)

        def visit_BoolOp(self, node):
            if isinstance(node.op, _ast.Or):
                # `x is None or x.attr` — left side is a null guard; right side
                # is only evaluated when left is False (x is not None).
                # Also handles `not x or x.attr` (not x → x is falsy/None).
                first = node.values[0]
                or_guarded: set[str] = set()
                if (
                    isinstance(first, _ast.Compare)
                    and isinstance(first.left, _ast.Name)
                    and len(first.ops) == 1
                    and isinstance(first.ops[0], _ast.Is)
                    and len(first.comparators) == 1
                    and isinstance(first.comparators[0], _ast.Constant)
                    and first.comparators[0].value is None
                    and first.left.id in self.optional_vars
                ):
                    or_guarded.add(first.left.id)
                elif (
                    isinstance(first, _ast.UnaryOp)
                    and isinstance(first.op, _ast.Not)
                    and isinstance(first.operand, _ast.Name)
                    and first.operand.id in self.optional_vars
                ):
                    or_guarded.add(first.operand.id)
                self.visit(first)
                for var in or_guarded:
                    self.guarded.add(var)
                for value in node.values[1:]:
                    self.visit(value)
                self.guarded -= or_guarded
                return
            if not isinstance(node.op, _ast.And):
                self.generic_visit(node)
                return
            # `x and x.attr` — each value is evaluated only when all preceding
            # values are truthy. If an optional_var appears as a bare Name, it's
            # guarded within subsequent values of the same And chain.
            # `isinstance(x, T) and x.attr` — isinstance acts as a type guard,
            # guaranteeing x is non-None (and of type T) in subsequent operands.
            newly_guarded: set[str] = set()
            for value in node.values:
                self.visit(value)
                if (
                    isinstance(value, _ast.Name)
                    and value.id in self.optional_vars
                    and value.id not in self.guarded
                ):
                    self.guarded.add(value.id)
                    newly_guarded.add(value.id)
                elif (
                    isinstance(value, _ast.Call)
                    and isinstance(value.func, _ast.Name)
                    and value.func.id == "isinstance"
                    and value.args
                    and isinstance(value.args[0], _ast.Name)
                    and value.args[0].id in self.optional_vars
                    and value.args[0].id not in self.guarded
                ):
                    # isinstance(x, T) implies x is not None
                    self.guarded.add(value.args[0].id)
                    newly_guarded.add(value.args[0].id)
            self.guarded -= newly_guarded

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
    # Test files: .first()/.get() without null checks is normal test-assertion
    # style; failures are caught by the test runner, not silent production crashes.
    if _is_test_file(call.file):
        return []
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
    # Click/Typer CLI commands: the decorator replaces the function so that
    # calling main() with no args reads from sys.argv. Not a missing-arg bug.
    if func.is_click_command:
        return []
    # `if __name__ == "__main__": main()` — entry point call, args come from
    # the runtime / argparse inside the function body, not the call site.
    if call.is_in_main_block:
        return []
    # Only non-kwonly required args can be covered by positional args.
    # Enumerate positional-only required args separately so a kwonly arg at
    # index i is never falsely marked covered because positional_count > i.
    positional_required = [a for a in func.required_args if not a.kwonly]
    # For method calls (obj.foo(args)), the receiver is implicit and not
    # counted in positional_count, but a module-level function with the same
    # name has 'self' (or equivalent first param) as an explicit required arg.
    # Add 1 so the receiver is treated as positionally covered.
    #
    # Numba/Bodo @intrinsic functions follow the same pattern: their first
    # parameter is `typingctx` (or `cgctx` / `ctx`), auto-injected by the
    # framework.  Callers within JIT impl() bodies never pass it explicitly.
    # Bodo uses "ctx" as its conventional short form for the same parameter.
    _NUMBA_CTX_PARAMS = frozenset({"typingctx", "cgctx", "context", "ctx"})
    is_numba_intrinsic = bool(
        positional_required
        and positional_required[0].name in _NUMBA_CTX_PARAMS
        and not call.is_method_call
    )
    effective_positional = call.positional_count + (
        1 if (call.is_method_call or is_numba_intrinsic) else 0
    )
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

_SAFE_SAVE_RECEIVER_KINDS = frozenset(
    {
        "form",  # Django ModelForm.save() — intentional full save
        "serializer",  # DRF serializer.save()
        "fs",  # FileSystemStorage
        "storage",  # Django storage backend
        "file",  # file-like object
        "input",  # allauth pattern: self.input is always a form
        "store",  # Django session/cache store (SessionBase subclass) — not an ORM model
    }
)


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
    _ORM_FETCH = frozenset(
        {
            "get",
            "first",
            "last",
            "latest",
            "earliest",
            "create",
            "get_or_create",
            "update_or_create",
            "bulk_create",
            "all",
            "filter",
            "exclude",
            "select_related",
            "prefetch_related",
        }
    )

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
                if (
                    isinstance(call_node.func, _ast.Attribute)
                    and call_node.func.attr == "save"
                ):
                    recv = call_node.func.value
                    recv_name: str | None = None
                    if isinstance(recv, _ast.Name):
                        recv_name = recv.id
                    elif isinstance(recv, _ast.Attribute) and isinstance(
                        recv.value, _ast.Name
                    ):
                        # self.something.save() — track on "something"
                        recv_name = recv.attr
                    if recv_name and constructed.get(recv_name):
                        new_obj_save_lines.add(call_node.lineno)

    return frozenset(new_obj_save_lines)


def _is_test_file(path: str) -> bool:
    """Return True if path looks like a test file (test_*.py, *_test.py, tests/ dir)."""
    import os as _os

    basename = _os.path.basename(path)
    return (
        basename.startswith("test_")
        or basename.endswith("_test.py")
        or basename
        == "test.py"  # base test-helper class (e.g. healthchecks/hc/test.py)
        or basename
        == "conftest.py"  # pytest fixture file — fixture setup, not production
        or "/test/" in path
        or "/tests/" in path
        or "/testing/" in path
    )


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
    # Test files: .save() calls are fixture setup, not concurrent-update races.
    if _is_test_file(call.file):
        return []
    # Positional args mean this is PIL/file/custom .save(path, format, ...) not Django.
    if call.positional_count >= 1 or call.has_var_args:
        return []
    # Django session backend: request.session is never an ORM model.
    # SessionBase.save() does not accept update_fields.
    if call.callee_name == "request.session.save":
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

    # Methods that mutate their receiver in place.
    _MUTATING_METHODS = frozenset(
        {
            "append",
            "extend",
            "update",
            "add",
            "remove",
            "discard",
            "pop",
            "clear",
            "insert",
            "reverse",
            "sort",
            "setdefault",
            "popitem",
            "__setitem__",
            "__delitem__",
        }
    )

    def _is_overload(func_node: _ast.FunctionDef | _ast.AsyncFunctionDef) -> bool:
        for dec in func_node.decorator_list:
            if isinstance(dec, _ast.Name) and dec.id == "overload":
                return True
            if isinstance(dec, _ast.Attribute) and dec.attr == "overload":
                return True
        return False

    def _param_is_mutated(
        func_node: _ast.FunctionDef | _ast.AsyncFunctionDef, param: str
    ) -> bool:
        """Return True if `param` is mutated anywhere in func_node's body.

        Checks subscript assignment (d[k]=v), mutating method calls
        (d.update(...)), augmented assignment (d |= x), and del d[k].
        Pure reads (if param: / return param) are not mutations.
        """
        for n in _ast.walk(func_node):
            # param.mutating_method(...)
            if (
                isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr in _MUTATING_METHODS
                and isinstance(n.func.value, _ast.Name)
                and n.func.value.id == param
            ):
                return True
            # param[key] = value
            if isinstance(n, _ast.Assign):
                for t in n.targets:
                    if (
                        isinstance(t, _ast.Subscript)
                        and isinstance(t.value, _ast.Name)
                        and t.value.id == param
                    ):
                        return True
            # param[key] += value  or  param |= {k: v}
            if isinstance(n, _ast.AugAssign):
                t = n.target
                if isinstance(t, _ast.Name) and t.id == param:
                    return True
                if (
                    isinstance(t, _ast.Subscript)
                    and isinstance(t.value, _ast.Name)
                    and t.value.id == param
                ):
                    return True
            # del param[key]
            if isinstance(n, _ast.Delete):
                for t in n.targets:
                    if (
                        isinstance(t, _ast.Subscript)
                        and isinstance(t.value, _ast.Name)
                        and t.value.id == param
                    ):
                        return True
        return False

    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if _is_overload(node):
            continue

        # Positional defaults map to the last N args (N = len(defaults)).
        args = node.args.args
        n_defaults = len(node.args.defaults)
        n_args = len(args)
        for i, default in enumerate(node.args.defaults):
            if not isinstance(default, _MUTABLE):
                continue
            param_name = args[n_args - n_defaults + i].arg
            if not _param_is_mutated(node, param_name):
                continue
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

        # Keyword-only defaults are one-to-one with kwonlyargs.
        for kwarg, kw_default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if kw_default is None or not isinstance(kw_default, _MUTABLE):
                continue
            if not _param_is_mutated(node, kwarg.arg):
                continue
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
    #
    # Names that appear as BOTH sync def and async def in the same file are
    # ambiguous (e.g. two closures with the same name inside different outer
    # functions). Skip them to avoid flagging calls to the sync version.
    sync_defined: set[str] = {
        node.name for node in _ast.walk(tree) if isinstance(node, _ast.FunctionDef)
    }
    # Sync method names: same-named sync def with self/cls — dual sync/async
    # client pattern (e.g. SyncClient.close + AsyncClient.close in one file).
    sync_method_names: set[str] = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, _ast.FunctionDef)
        and node.args.args
        and node.args.args[0].arg in ("self", "cls")
    }

    def _has_work_decorator(func_node: _ast.AsyncFunctionDef) -> bool:
        """Return True if func_node is decorated with @work or @work(...).

        Textual's @work decorator wraps async methods into synchronous worker
        dispatch — callers call them without await.
        """
        for dec in func_node.decorator_list:
            name = None
            if isinstance(dec, _ast.Name):
                name = dec.id
            elif isinstance(dec, _ast.Attribute):
                name = dec.attr
            elif isinstance(dec, _ast.Call):
                if isinstance(dec.func, _ast.Name):
                    name = dec.func.id
                elif isinstance(dec, _ast.Call) and isinstance(
                    dec.func, _ast.Attribute
                ):
                    name = dec.func.attr
            if name == "work":
                return True
        return False

    # First pass: collect ids of AsyncFunctionDef nodes that are nested inside
    # another function or method body.  Nested async closures are not callable
    # by bare name from outside their enclosing scope, so adding them to
    # module_async causes FPs when the same name is used as a loop variable,
    # import alias, or is intentionally returned as an awaitable.
    nested_async_ids: set[int] = set()
    for outer in _ast.walk(tree):
        if isinstance(outer, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for inner in _ast.walk(outer):
                if inner is not outer and isinstance(
                    inner, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                ):
                    nested_async_ids.add(id(inner))

    # Second pass: collect async generator names (contain yield/yield from) at
    # any scope — used to exclude names that are reused as both generator and
    # coroutine from module_async.
    async_generator_names: set[str] = {
        node.name
        for node in _ast.walk(tree)
        if isinstance(node, _ast.AsyncFunctionDef)
        and any(isinstance(n, (_ast.Yield, _ast.YieldFrom)) for n in _ast.walk(node))
    }
    module_async: set[str] = set()
    method_async: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.AsyncFunctionDef):
            # Skip closures — their names are only valid in the enclosing scope.
            if id(node) in nested_async_ids:
                continue
            # Async generators (contain yield) return AsyncGenerator, not a coroutine.
            # Calling them without await is correct; they're consumed via `async for`.
            if any(
                isinstance(n, (_ast.Yield, _ast.YieldFrom)) for n in _ast.walk(node)
            ):
                continue
            # Heuristic: if the first argument is self/cls, it's a method
            args = node.args.args
            if args and args[0].arg in ("self", "cls"):
                # Skip if the same method name also exists as a sync def —
                # dual sync/async client pattern; pact can't resolve which class
                # self refers to and would cross-contaminate (e.g. openai-python).
                # Also skip methods decorated with @work / @work(...) —
                # Textual's @work wraps async methods into synchronous worker
                # dispatch; callers invoke them without await intentionally.
                if node.name not in sync_method_names and not _has_work_decorator(node):
                    method_async.add(node.name)
            else:
                # Skip names that also have a sync def — closure-level name
                # collision; pact cannot resolve which definition is called.
                # Also skip names used as async generators elsewhere in the file —
                # pact cannot resolve which definition is called at each call site.
                if (
                    node.name not in sync_defined
                    and node.name not in async_generator_names
                ):
                    module_async.add(node.name)

    if not module_async and not method_async:
        return []

    # Detect asyncio consumer names imported directly: `from asyncio import run`.
    _ASYNCIO_CONSUMER_SOURCE_NAMES = frozenset(
        {
            "run",
            "ensure_future",
            "gather",
            "wait",
            "wait_for",
            "shield",
            "run_coroutine_threadsafe",
        }
    )
    file_imported_consumers: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom) and node.module == "asyncio":
            for alias in node.names:
                if alias.name in _ASYNCIO_CONSUMER_SOURCE_NAMES:
                    file_imported_consumers.add(alias.asname or alias.name)

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
            "run_worker",  # Textual UI framework: schedules coroutine as worker
            "call_soon",  # asyncio loop scheduling
            "call_soon_threadsafe",
            "asyncio_run",  # user-defined wrapper: def asyncio_run(f): loop.run_until_complete(f)
            "run_async",  # alternative user-defined wrapper name
            "schedule",  # custom task schedulers (e.g. StreamTransformer.schedule(coro))
            "run_until_complete",  # event_loop.run_until_complete(coro()) — any receiver name
            "run_sync",  # Chainlit sync wrapper: run_sync(coro()) blocks until done
            "as_completed",  # asyncio.as_completed([coro() for ...]) — iterates awaitables
            "start_soon",  # AnyIO/trio TaskGroup.start_soon(func, coro) — schedules task
            "spawn",  # Trio nursery.spawn() — legacy name for start_soon
            "start_background_task",  # some async frameworks
            "start_task",  # custom task manager wrappers: self.start_task(coro()) → loop.create_task(coro)
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
            if (
                fname in _CORO_CONSUMERS
                or fname in file_imported_consumers
                or (fname is not None and fname.startswith("create_task"))
            ):
                return True
            # Qualified consumers: asyncio.run(), loop.run_until_complete()
            if receiver is not None and (receiver, fname) in _CORO_CONSUMERS_QUALIFIED:
                return True
        # Collected into a list/tuple that will be passed to gather et al:
        # tasks = [coro1(), coro2()] or gather([coro1(), coro2()])
        if isinstance(parent, (_ast.List, _ast.Tuple, _ast.Set)):
            gp = parent_map.get(id(parent))
            if isinstance(gp, _ast.Call):
                func = gp.func
                fname = (
                    func.attr
                    if isinstance(func, _ast.Attribute)
                    else func.id if isinstance(func, _ast.Name) else None
                )
                if (
                    fname in _CORO_CONSUMERS
                    or fname in file_imported_consumers
                    or (fname is not None and fname.startswith("create_task"))
                ):
                    return True
                # list.append((coro(), metadata)) — tuple wrapping coroutine before gather
                if isinstance(func, _ast.Attribute) and func.attr == "append":
                    return True
            # tasks = [coro1(), coro2(), coro3()] — list literal assigned to a
            # variable for later gather(*tasks). Same intent as list comprehension.
            if isinstance(gp, (_ast.Assign, _ast.AnnAssign, _ast.AugAssign)):
                return True
        # .append(coro()) — common pattern before asyncio.gather(*tasks)
        if isinstance(parent, _ast.Call):
            if isinstance(parent.func, _ast.Attribute) and parent.func.attr == "append":
                return True
        # tasks = [coro(item) for item in items] — batch/gather collection pattern.
        # The call is the `elt` of a list/set/generator comprehension that is assigned
        # to a variable (not discarded as an expression statement).
        if isinstance(parent, (_ast.ListComp, _ast.SetComp, _ast.GeneratorExp)):
            gp = parent_map.get(id(parent))
            if isinstance(gp, (_ast.Assign, _ast.AnnAssign, _ast.AugAssign)):
                return True
            # return (coro(item) for item in items) — lazy generator returned to
            # caller for gather; same intent as list comprehension assigned to var.
            if isinstance(gp, _ast.Return):
                return True
            # asyncio.as_completed([coro() for ...]) — list-comp directly as arg
            if isinstance(gp, _ast.Call):
                _func = gp.func
                _fname = (
                    _func.attr
                    if isinstance(_func, _ast.Attribute)
                    else _func.id if isinstance(_func, _ast.Name) else None
                )
                _recv = (
                    _func.value.id
                    if isinstance(_func, _ast.Attribute)
                    and isinstance(_func.value, _ast.Name)
                    else None
                )
                if (
                    _fname in _CORO_CONSUMERS
                    or _fname in file_imported_consumers
                    or (_fname is not None and _fname.startswith("create_task"))
                ):
                    return True
                if _recv is not None and (_recv, _fname) in _CORO_CONSUMERS_QUALIFIED:
                    return True
                # list.extend(coro() for ...) — collecting coroutines for later gather
                if isinstance(_func, _ast.Attribute) and _func.attr == "extend":
                    return True
            # asyncio.gather(*[coro(item) for item in items])
            if isinstance(gp, _ast.Starred):
                ggp = parent_map.get(id(gp))
                if isinstance(ggp, _ast.Call):
                    func = ggp.func
                    fname = (
                        func.attr
                        if isinstance(func, _ast.Attribute)
                        else func.id if isinstance(func, _ast.Name) else None
                    )
                    if fname in _CORO_CONSUMERS or fname in file_imported_consumers:
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
            "__await__",
            "__aiter__",
            "__anext__",
        ):
            return True
        # lambda ...: coro() — sync lambda returning coroutine for caller to await.
        # e.g. embedding_fn = lambda query: generate_embeddings(query)
        if isinstance(parent, _ast.Lambda):
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
        # Strip escaped-brace sequences {{...}} before parsing placeholders —
        # {{body}} is a literal "{body}" in the output, not a format field.
        _scrubbed = fmt_str.replace("{{", "\x00").replace("}}", "\x00")
        auto_count = len(_re.findall(r"\{\}", _scrubbed))
        indexed = {int(m) for m in _re.findall(r"\{(\d+)\}", _scrubbed)}
        named = set(
            _re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_]\w*)*)\}", _scrubbed)
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
        # A name like "self.tx_ac" is covered if its root "self" is in kw_keys,
        # because .format(self=obj) resolves {self.tx_ac} by attribute lookup.
        missing_names = {
            n for n in named if n not in kw_keys and n.split(".")[0] not in kw_keys
        }
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

        def visit_IfExp(self, node):
            # Ternary: body if test else orelse.
            # If the test mentions an llm_var, the body branch is guarded.
            # E.g. `response.choices[0] if response.choices else None` is safe.
            self.visit(node.test)
            src = _ast.unparse(node.test) if hasattr(_ast, "unparse") else ""
            newly_guarded: set[str] = set()
            for var in list(llm_vars):
                if var in src and var not in guarded:
                    guarded.add(var)
                    newly_guarded.add(var)
            self.visit(node.body)
            for var in newly_guarded:
                guarded.discard(var)
            self.visit(node.orelse)

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
            # from collections import defaultdict → defaultdict(list) — bare Name
            if isinstance(value.func, pyast.Name) and value.func.id == "defaultdict":
                self._defaultdicts.add(target_id)
                return
            if not isinstance(value.func, pyast.Attribute):
                return
            if value.func.attr == "defaultdict":
                self._defaultdicts.add(target_id)
            elif value.func.attr == "get":
                self._get_vars[target_id] = line

        def _clear_for_target(self, target: pyast.expr) -> None:
            """Untrack a variable when it's rebound by a for-loop target."""
            if isinstance(target, pyast.Name):
                self._get_vars.pop(target.id, None)
                self._guarded.pop(target.id, None)
            elif isinstance(target, (pyast.Tuple, pyast.List)):
                for elt in target.elts:
                    self._clear_for_target(elt)

        def visit_Assign(self, node: pyast.Assign) -> None:
            if len(node.targets) == 1 and isinstance(node.targets[0], pyast.Name):
                self._classify_assign(node.targets[0].id, node.value, node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: pyast.AnnAssign) -> None:
            # x: SomeType = collections.defaultdict(...) — annotated assignment
            if isinstance(node.target, pyast.Name) and node.value is not None:
                self._classify_assign(node.target.id, node.value, node.lineno)
            self.generic_visit(node)

        def visit_For(self, node: pyast.For) -> None:
            # for x in iterable: — x is now from iteration, not from .get()
            self._clear_for_target(node.target)
            self.generic_visit(node)

        def visit_Compare(self, node: pyast.Compare) -> None:
            # x in other_dict — record the guard
            if (
                len(node.ops) == 1
                and isinstance(node.ops[0], (pyast.In, pyast.NotIn))
                and isinstance(node.left, pyast.Name)
                and node.left.id in self._get_vars
            ):
                var = node.left.id
                for comparator in node.comparators:
                    if isinstance(comparator, pyast.Name):
                        self._guarded.setdefault(var, set()).add(comparator.id)
                    elif isinstance(comparator, (pyast.List, pyast.Tuple, pyast.Set)):
                        # if var in [c1, c2, ...] — membership in a non-None constant
                        # collection guarantees var is a specific non-None value.
                        if all(
                            isinstance(elt, pyast.Constant) and elt.value is not None
                            for elt in comparator.elts
                        ):
                            self._guarded.setdefault(var, set()).add("__any__")
            self.generic_visit(node)

        def visit_Call(self, node: pyast.Call) -> None:
            # isinstance(var, T) — var is guaranteed non-None in this branch
            if (
                isinstance(node.func, pyast.Name)
                and node.func.id == "isinstance"
                and node.args
                and isinstance(node.args[0], pyast.Name)
                and node.args[0].id in self._get_vars
            ):
                self._guarded.setdefault(node.args[0].id, set()).add("__any__")
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
            if isinstance(node.ctx, pyast.Store):
                # Dict write: collection[var] = ... — never raises KeyError.
                # Also serves as an implicit guard: var is now in collection for
                # subsequent reads (e.g. collection[var]["sub"] = ... on next line).
                self._guarded.setdefault(var, set()).add(collection)
                self.generic_visit(node)
                return
            guarded_against = self._guarded.get(var, set())
            if collection not in guarded_against and "__any__" not in guarded_against:
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
