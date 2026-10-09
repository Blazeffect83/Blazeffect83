"""Quality bar: the runtime may import only the standard library, numpy and itself.

Scans every module under ``polymath/`` (AST, so lazy/conditional imports are
caught too) and also checks that the installed runtime requirements list
nothing but numpy.
"""

from __future__ import annotations

import ast
import sys
import tomllib

from tests.conftest import ROOT

ALLOWED_THIRD_PARTY = {"numpy"}
FORBIDDEN_EXAMPLES = {
    "torch",
    "tensorflow",
    "sklearn",
    "spacy",
    "nltk",
    "fastapi",
    "flask",
    "trafilatura",
    "sqlite_vec",
    "transformers",
    "sentence_transformers",
    "onnxruntime",
    "llama_cpp",
    "openai",
    "anthropic",
    "requests",
    "httpx",
    "bs4",
    "lxml",
    "gensim",
    "pandas",
    "scipy",
}


def runtime_imports() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted((ROOT / "polymath").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names = [arg.value]
            for name in names:
                found.setdefault(name.split(".")[0], set()).add(str(path.relative_to(ROOT)))
    return found


def test_only_stdlib_numpy_and_self_are_imported():
    stdlib = set(sys.stdlib_module_names)
    offenders = {
        top: files
        for top, files in runtime_imports().items()
        if top not in stdlib and top not in ALLOWED_THIRD_PARTY and top != "polymath"
    }
    assert not offenders, f"non-stdlib runtime imports: {offenders}"


def test_no_importlib_dynamic_third_party():
    for path in (ROOT / "polymath").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import_module(" not in text, f"dynamic import in {path}"


def test_declared_runtime_dependencies_are_numpy_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = [d.split(">")[0].split("=")[0].split("<")[0].strip().lower() for d in project["dependencies"]]
    assert deps == ["numpy"]


def test_forbidden_examples_are_really_forbidden():
    # Guards the guard: none of the banned frameworks are stdlib modules.
    assert not FORBIDDEN_EXAMPLES & set(sys.stdlib_module_names)
