#!/usr/bin/env python3
"""
generate_api_doc.py

Automatically discovers **all public callables** (functions and classes)
in a given Python package / module and writes a markdown reference file
named ``API_DOCUMENTATION.md``.

Features
--------
* Recursively walks sub‑packages.
* Ignores private names that start with ``_``.
* Shows parameter name, type hint, default value, and “required?” flag.
* Generates a tiny “usage” code stub for each callable.
* Includes a small ``validate_parameters`` utility for runtime checks.

Usage
-----
    # From the repo root
    python generate_api_doc.py path/to/your/package   # or a single .py file

    # You can also import the helper functions in your own code:
    from generate_api_doc import list_parameters, validate_parameters
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, Mapping, Tuple, List, Dict

# ----------------------------------------------------------------------
# Helpers – formatting --------------------------------------------------
# ----------------------------------------------------------------------


def _is_public_name(name: str) -> bool:
    """Return True for names that are not private (no leading underscore)."""
    return not name.startswith("_")


def _format_default(val: Any) -> str:
    """Human‑readable representation of a default value."""
    if val is inspect.Parameter.empty:
        return ""
    # Simple scalars are shown as‑is; containers are JSON‑ified (truncated if huge)
    if isinstance(val, (str, int, float, bool)):
        return repr(val)
    try:
        txt = json.dumps(val, default=str)
        return txt if len(txt) < 60 else txt[:57] + "..."
    except Exception:
        return repr(val)


def _type_hint_str(param: inspect.Parameter) -> str:
    """Return a concise string for a type hint (or empty string)."""
    if param.annotation is inspect.Parameter.empty:
        return ""
    # Strip the verbose ``typing.`` prefix for readability
    return str(param.annotation).replace("typing.", "")


# ----------------------------------------------------------------------
# Core introspection ----------------------------------------------------
# ----------------------------------------------------------------------


def list_parameters(callable_obj: Callable) -> List[Tuple[str, str, str, bool]]:
    """
    Return a list of (name, type_hint, default_repr, required) for *callable_obj*.

    If ``callable_obj`` is a class, its ``__init__`` signature is inspected.
    ``self``/``cls`` are omitted automatically.
    """
    if inspect.isclass(callable_obj):
        # Skip built‑in ``object.__init__`` (no useful params)
        sig = inspect.signature(callable_obj.__init__)
    else:
        sig = inspect.signature(callable_obj)

    params: List[Tuple[str, str, str, bool]] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        required = param.default is inspect.Parameter.empty
        default_repr = _format_default(param.default)
        type_str = _type_hint_str(param)
        params.append((name, type_str, default_repr, required))
    return params


def print_parameters_table(callable_obj: Callable, display_name: str) -> None:
    """Print a simple ASCII table of the callable’s signature (useful for debugging)."""
    rows = list_parameters(callable_obj)
    if not rows:
        print(f"{display_name}: <no parameters>")
        return

    # Determine column widths
    name_w = max(len(r[0]) for r in rows)
    type_w = max(len(r[1]) for r in rows)
    default_w = max(len(r[2]) for r in rows)
    header = f"{'Parameter':{name_w}}  {'Type':{type_w}}  {'Default':{default_w}}  Required"
    print(header)
    print("-" * len(header))
    for pname, ptype, pdef, req in rows:
        req_str = "Yes" if req else "No"
        print(f"{pname:{name_w}}  {ptype:{type_w}}  {pdef:{default_w}}  {req_str}")
    print()


def discover_callables(root_module: types.ModuleType) -> Dict[str, Callable]:
    """
    Recursively walk a module / package and return a mapping:

        {qualified_name: callable_object}

    Only public functions and classes are collected.
    """
    discovered: Dict[str, Callable] = {}

    def _walk(mod: types.ModuleType, prefix: str = "") -> None:
        for name, obj in vars(mod).items():
            if not _is_public_name(name):
                continue

            qualified = f"{prefix}.{name}" if prefix else name

            if inspect.isfunction(obj):
                discovered[qualified] = obj
            elif inspect.isclass(obj):
                discovered[qualified] = obj
                # Classes can have nested sub‑modules – walk them too
                # (e.g., ``class Foo: from . import bar``).  We ignore class attributes.
            elif inspect.ismodule(obj):
                # Recurse only into sub‑modules that belong to the same top‑level package
                if obj.__package and obj.__package__.startswith(root_module.__package__):
                    _walk(obj, qualified)

    _walk(root_module)
    return discovered


# ----------------------------------------------------------------------
# Markdown rendering ----------------------------------------------------
# ----------------------------------------------------------------------


def _markdown_table(rows: List[Tuple[str, str, str, bool]]) -> str:
    """Return a markdown table string for a list of parameter tuples."""
    if not rows:
        return "_No parameters_\n"

    header = "| Parameter | Type | Default | Required |\n|---|---|---|---|"
    body = "\n".join(
        f"| `{name}` | `{type_}` | `{default}` | {'Yes' if required else 'No'} |"
        for name, type_, default, required in rows
    )
    return f"{header}\n{body}\n"


def _usage_stub(name: str, params: List[Tuple[str, str, str, bool]]) -> str:
    """Create a tiny call‑example like ``func(arg1, arg2)``."""
    arg_names = [p[0] for p in params]
    return f"{name}({', '.join(arg_names)})"


def generate_markdown(
    callables: Dict[str, Callable],
    out_path: Path = Path("API_DOCUMENTATION.md"),
) -> None:
    """
    Write a markdown file that documents every callable in *callables*.

    Parameters
    ----------
    callables : dict
        Mapping ``{qualified_name: callable}``.
    out_path : Path, optional
        Destination file (defaults to ``API_DOCUMENTATION.md`` in the cwd).
    """
    lines: List[str] = [
        "# API Documentation",
        "",
        "*Generated automatically by* `generate_api_doc.py`",
        "",
    ]

    for qname in sorted(callables):
        obj = callables[qname]
        param_rows = list_parameters(obj)

        lines.append(f"## `{qname}`")
        lines.append("")
        # Short one‑liner description – try to pull the docstring first line
        doc =
