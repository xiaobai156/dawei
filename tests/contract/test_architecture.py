from __future__ import annotations

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "dawei"
LAYERS = {"domain", "infrastructure", "parsers", "application", "cli"}
ALLOWED = {
    "domain": {"domain"},
    "infrastructure": {"domain", "infrastructure"},
    "parsers": {"domain", "parsers"},
    "application": {"domain", "infrastructure", "parsers", "application"},
    "cli": {"domain", "application", "cli"},
}


def module_name(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def absolute_import(current: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = current.split(".")[:-1]
    if node.level > len(package):
        return None
    prefix = package[: len(package) - node.level + 1]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def dawei_imports(path: Path) -> set[str]:
    current = module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names if alias.name.startswith("dawei."))
        elif isinstance(node, ast.ImportFrom):
            imported = absolute_import(current, node)
            if imported and imported.startswith("dawei"):
                imports.add(imported)
    return imports


class ArchitectureContractTests(unittest.TestCase):
    def test_root_python_entries_are_thin_cli_delegates(self) -> None:
        entries = (
            "scrape_all_36.py",
            "detect_duplicate_sites.py",
            "validate_failed_sites_36.py",
        )
        violations: list[str] = []
        for filename in entries:
            path = PROJECT_ROOT / filename
            source = path.read_text(encoding="utf-8-sig")
            if len(source.splitlines()) > 50:
                violations.append(f"{filename}: exceeds 50 lines")
            ast.parse(source, filename=str(path))
            imports = dawei_imports(path)
            if any(not name.startswith("dawei.cli") for name in imports):
                violations.append(f"{filename}: imports outside dawei.cli: {sorted(imports)}")
        self.assertEqual(violations, [])

    def test_secondary_entries_do_not_import_scrape_main(self) -> None:
        violations: list[str] = []
        for filename in ("detect_duplicate_sites.py", "validate_failed_sites_36.py"):
            path = PROJECT_ROOT / filename
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = {node.module}
                else:
                    continue
                if "scrape_all_36" in imported:
                    violations.append(f"{filename}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_layer_dependencies_only_point_in_allowed_directions(self) -> None:
        violations: list[str] = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            relative = path.relative_to(PACKAGE_ROOT)
            source_layer = relative.parts[0] if len(relative.parts) > 1 else None
            if source_layer not in LAYERS:
                continue
            for imported in dawei_imports(path):
                parts = imported.split(".")
                target_layer = parts[1] if len(parts) > 1 else None
                if target_layer in LAYERS and target_layer not in ALLOWED[source_layer]:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}: {source_layer} -> {target_layer} ({imported})"
                    )
        self.assertEqual(violations, [])

    def test_domain_does_not_import_io_libraries(self) -> None:
        forbidden = {"urllib", "http", "socket", "ssl", "subprocess", "playwright"}
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "domain").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module.split(".")[0]}
                else:
                    continue
                if names & forbidden:
                    violations.append(f"{path.name}: {sorted(names & forbidden)}")
        self.assertEqual(violations, [])

    def test_dawei_modules_have_no_import_cycles(self) -> None:
        paths = tuple(PACKAGE_ROOT.rglob("*.py"))
        modules = {module_name(path): path for path in paths}
        graph = {
            module: {name for name in dawei_imports(path) if name in modules}
            for module, path in modules.items()
        }
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(module: str) -> None:
            if module in visiting:
                cycle = visiting[visiting.index(module) :] + [module]
                self.fail("import cycle: " + " -> ".join(cycle))
            if module in visited:
                return
            visiting.append(module)
            for dependency in graph[module]:
                visit(dependency)
            visiting.pop()
            visited.add(module)

        for module in graph:
            visit(module)

    def test_parser_dispatch_never_branches_on_site_name(self) -> None:
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "parsers").rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.IfExp)):
                    continue
                if any(
                    isinstance(child, ast.Attribute) and child.attr == "name"
                    for child in ast.walk(node.test)
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} branches on site name"
                    )
        self.assertEqual(violations, [])

    def test_parsers_do_not_import_network_browser_or_process_libraries(self) -> None:
        forbidden = {
            "urllib",
            "http",
            "socket",
            "ssl",
            "subprocess",
            "playwright",
            "requests",
        }
        violations: list[str] = []
        for path in (PACKAGE_ROOT / "parsers").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module.split(".")[0]}
                else:
                    continue
                if names & forbidden:
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)}: {sorted(names & forbidden)}"
                    )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
