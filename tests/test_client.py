"""Focused tests for custom_components.perfectly_snug.client."""

import importlib.util
import sys
import types
from pathlib import Path


PACKAGE_ROOT = (
    Path(__file__).parent.parent / "custom_components" / "perfectly_snug"
)


custom_components_pkg = types.ModuleType("custom_components")
custom_components_pkg.__path__ = [str(PACKAGE_ROOT.parent)]
sys.modules.setdefault("custom_components", custom_components_pkg)

perfectly_snug_pkg = types.ModuleType("custom_components.perfectly_snug")
perfectly_snug_pkg.__path__ = [str(PACKAGE_ROOT)]
sys.modules.setdefault("custom_components.perfectly_snug", perfectly_snug_pkg)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


const_module = _load_module(
    "custom_components.perfectly_snug.const",
    PACKAGE_ROOT / "const.py",
)
client_module = _load_module(
    "custom_components.perfectly_snug.client",
    PACKAGE_ROOT / "client.py",
)


class TestMissingSettingsClassification:
    def test_tolerates_small_noncritical_partial_response(self):
        setting_ids = list(range(100, 125))
        missing = [101, 103, 105]

        assert client_module.TopperClient._missing_settings_are_fatal(setting_ids, missing) is False

    def test_fails_when_critical_setting_missing(self):
        setting_ids = const_module.POLL_SETTINGS
        missing = [const_module.SETTING_L1]

        assert client_module.TopperClient._missing_settings_are_fatal(setting_ids, missing) is True

    def test_fails_when_too_many_settings_missing(self):
        setting_ids = list(range(100, 125))
        missing = [100, 101, 102, 103, 104, 105]

        assert client_module.TopperClient._missing_settings_are_fatal(setting_ids, missing) is True
