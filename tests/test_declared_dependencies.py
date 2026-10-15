"""Every third-party import in src/ and tests/ must be a declared dependency.

On 2026-10-10 and 10-11 the scheduled live-checks ran on a clean runner, which
installs only what pyproject.toml declares. tests/test_maintenance_workflow.py
imported `yaml` at module level, PyYAML was never declared, and collection
died with pytest exit code 2. The live suite never ran, so the job that
watches settlement stations and MOS availability watched nothing for three
days, and paged "LIVE CHECKS FAILED" with the contract-change text.

A laptop with the package already installed can never see this. Only a
comparison of imports against declarations can.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from importlib import metadata


ROOTS = ("src", "tests")
LOCAL = {"src", "tests", "deploy", "research"}


def _declared() -> set:
    """Distribution names from pyproject: dependencies + the dev extra."""
    text = pathlib.Path("pyproject.toml").read_text()
    try:
        import tomllib
    except ModuleNotFoundError:          # Python < 3.11 (local dev only)
        import re

        names = re.findall(r'^\s*"([A-Za-z0-9_.-]+)', text, re.M)
        return {n.lower().replace("_", "-") for n in names}
    data = tomllib.loads(text)
    reqs = list(data["project"]["dependencies"])
    reqs += data["project"].get("optional-dependencies", {}).get("dev", [])
    out = set()
    for r in reqs:
        name = r.split(";")[0].split("[")[0]
        for sep in "<>=!~ ":
            name = name.split(sep)[0]
        out.add(name.strip().lower().replace("_", "-"))
    return out


def _top_level_imports():
    for root in ROOTS:
        for path in pathlib.Path(root).rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        yield path, alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    yield path, node.module.split(".")[0]


def _owners() -> dict:
    """module -> [distribution]. packages_distributions() is 3.10+; this is the
    same mapping built by hand for the local 3.9 interpreter."""
    if hasattr(metadata, "packages_distributions"):
        return metadata.packages_distributions()
    owners: dict = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        top = (dist.read_text("top_level.txt") or "").split()
        if not top:
            top = {f.parts[0].split(".")[0] for f in (dist.files or []) if f.parts}
        for module in top:
            owners.setdefault(module, []).append(name)
    return owners


def _is_stdlib(module: str) -> bool:
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return module in names
    import importlib.util
    import sysconfig

    if module in sys.builtin_module_names:
        return True
    spec = importlib.util.find_spec(module)
    origin = getattr(spec, "origin", None) or ""
    return bool(origin) and origin.startswith(sysconfig.get_paths()["stdlib"]) \
        and "site-packages" not in origin


def test_every_third_party_import_is_declared():
    owners = _owners()
    declared = _declared() | {"pytest", "starlette", "anyio", "sqlalchemy"}
    missing = {}
    for path, module in _top_level_imports():
        if module in LOCAL or module.startswith("_") or module in ("tomllib",) or _is_stdlib(module):
            continue
        dists = {d.lower().replace("_", "-") for d in owners.get(module, [module])}
        if not dists & declared:
            missing.setdefault(module, str(path))
    assert not missing, f"imported but not declared in pyproject.toml: {missing}"
