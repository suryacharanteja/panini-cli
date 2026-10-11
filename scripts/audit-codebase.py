"""Offline inventory of first-party modules and the installed runtime dependency closure."""

import ast
import importlib.metadata
import json
import tomllib
from pathlib import Path

from packaging.requirements import Requirement


def main():
    repo = Path(__file__).resolve().parents[1]
    output = repo / "build/audit"
    output.mkdir(parents=True, exist_ok=True)
    modules = []
    for path in sorted((repo / "src/panini_cli").rglob("*.py")):
        if "panini-sandbox" in path.parts:
            continue
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=str(path))
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        modules.append({"path": path.relative_to(repo).as_posix(), "lines": len(text.splitlines()),
                        "functions": len(functions),
                        "complexFunctions": sorted(({
                            "name": node.name, "line": node.lineno,
                            "lines": node.end_lineno - node.lineno + 1,
                            "decisionPoints": sum(isinstance(child, (ast.If, ast.For, ast.While,
                                ast.ExceptHandler, ast.IfExp, ast.comprehension)) for child in ast.walk(node)),
                        } for node in functions), key=lambda row: row["decisionPoints"], reverse=True)[:3],
                        "largestFunctionLines": max(
                            (node.end_lineno - node.lineno + 1 for node in functions), default=0)})
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    remaining = list(project["project"]["dependencies"])
    distributions = {}
    while remaining:
        requirement = Requirement(remaining.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = requirement.name.lower().replace("_", "-")
        if name in distributions:
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        distributions[name] = distribution.version
        remaining.extend(distribution.requires or [])
    (output / "requirements-runtime.txt").write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(distributions.items())))
    inventory = {"moduleCount": len(modules), "sourceLines": sum(row["lines"] for row in modules),
                 "modules": modules, "runtimeDependencies": distributions}
    (output / "inventory.json").write_text(json.dumps(inventory, indent=2))
    print(json.dumps({"moduleCount": inventory["moduleCount"], "sourceLines": inventory["sourceLines"],
                      "largestModules": sorted(modules, key=lambda row: row["lines"], reverse=True)[:6],
                      "runtimeDependencies": distributions}, indent=2))


if __name__ == "__main__":
    main()
