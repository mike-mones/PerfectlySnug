"""Regression tests for code-hygiene cleanup decisions."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module_tree(relative_path: str) -> ast.Module:
    return ast.parse((ROOT / relative_path).read_text())


def _const_import_names(relative_path: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_module_tree(relative_path)):
        if isinstance(node, ast.ImportFrom) and node.module == "const":
            names.update(alias.name for alias in node.names)
    return names


def test_no_stale_right_rail_release_constants():
    tree = _module_tree("appdaemon/sleep_controller_v5.py")
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "RIGHT_RAIL_RELEASE_F" not in assigned
    assert "RIGHT_RAIL_RELEASE_TOLERANCE_F" not in assigned
    assert "E_RIGHT_OVERHEAT_RAIL_FLAG" not in assigned


def test_compute_setting_has_no_unused_body_max_assignment():
    tree = _module_tree("appdaemon/sleep_controller_v5.py")
    compute_setting = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_compute_setting"
    )

    assigned = {
        target.id
        for node in ast.walk(compute_setting)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "body_max" not in assigned


def test_init_does_not_import_unused_domain():
    assert "DOMAIN" not in _const_import_names("custom_components/perfectly_snug/__init__.py")


def test_coordinator_does_not_import_unused_room_temp_const():
    names = _const_import_names("custom_components/perfectly_snug/coordinator.py")
    assert "CONF_ROOM_TEMP_ENTITY" not in names
