"""
Unit tests for sleep_controller_v2.py

These tests verify critical invariants that, if broken, cause
silent failures (like amnesia bugs or disconnected variables).
Run from the repo root:
    python3 -m pytest PerfectlySnug/tests/test_controller_v2.py -v
"""

import ast
import json
import re
from pathlib import Path

CONTROLLER_PATH = Path(__file__).parent.parent / "appdaemon" / "sleep_controller_v2.py"
TRAINER_PATH = Path(__file__).parent.parent / "ml" / "train_stage_classifier.py"
STATE_PATH = Path(__file__).parent.parent / "ml" / "state" / "controller_state.json"


def _read_controller_source():
    return CONTROLLER_PATH.read_text()


class TestHealthEntitiesWired:
    """Every entity defined in HEALTH_ENTITIES must actually be
    read and used in the control loop. Catches the 'defined but
    never used' bug that silently ignored respiratory_rate and
    wrist_temp for a week."""

    def test_all_health_entities_are_read(self):
        """Every key in HEALTH_ENTITIES must have a corresponding
        _read_entity(HEALTH_ENTITIES[...]) or get_state() call."""
        src = _read_controller_source()

        # Parse HEALTH_ENTITIES dict
        match = re.search(
            r'HEALTH_ENTITIES\s*=\s*\{([^}]+)\}', src, re.DOTALL
        )
        assert match, "Cannot find HEALTH_ENTITIES in controller"

        entity_block = match.group(1)
        keys = re.findall(r'"(\w+)":', entity_block)
        assert len(keys) >= 4, f"Expected ≥4 health entities, got {keys}"

        # Check each key is actually referenced in a _read_entity call
        for key in keys:
            pattern = rf'HEALTH_ENTITIES\["{key}"\]'
            uses = re.findall(pattern, src)
            assert len(uses) >= 1, (
                f"HEALTH_ENTITIES[\"{key}\"] is defined but never "
                f"read with _read_entity(). This is a silent "
                f"failure — the variable is being ignored."
            )

    def test_respiratory_rate_used_in_stage_estimation(self):
        """resp_rate must be passed to _estimate_stage_from_hr."""
        src = _read_controller_source()
        assert "resp_rate" in src, "resp_rate variable not found"
        # Check it appears in the heuristic or ML features
        assert re.search(r'resp_rate_pct', src), (
            "resp_rate_pct not computed — respiratory rate is "
            "not being used for stage classification"
        )

    def test_wrist_temp_tracked(self):
        """wrist_temp must be read and stored."""
        src = _read_controller_source()
        assert re.search(
            r'_read_entity\(HEALTH_ENTITIES\["wrist_temp"\]\)', src
        ), "wrist_temp is not being read from HA"


class TestStatePersistence:
    """Verify that learned data survives controller restarts.
    Catches the 'amnesia bug' where _load_state() created
    fresh state and ignored saved training data."""

    def test_load_state_restores_training_data(self):
        """_load_state must merge saved stage_training_data
        back into zone_state, not discard it."""
        src = _read_controller_source()
        # The load function must reference stage_training_data
        load_fn = _extract_method(src, "_load_state")
        assert "stage_training_data" in load_fn, (
            "_load_state() does not restore stage_training_data. "
            "Training samples will be lost on every restart!"
        )

    def test_load_state_restores_setting_change_log(self):
        """_load_state must merge saved setting_change_log."""
        src = _read_controller_source()
        load_fn = _extract_method(src, "_load_state")
        assert "setting_change_log" in load_fn, (
            "_load_state() does not restore setting_change_log. "
            "Transfer function learning data lost on restart!"
        )

    def test_save_state_includes_zone_state(self):
        """_save_state must write the full zone_state dict."""
        src = _read_controller_source()
        save_fn = _extract_method(src, "_save_state")
        assert "zone_state" in save_fn, (
            "_save_state() does not persist zone_state"
        )

    def test_save_state_includes_learned_targets(self):
        """_save_state must write learned_targets."""
        src = _read_controller_source()
        save_fn = _extract_method(src, "_save_state")
        assert "learned_targets" in save_fn, (
            "_save_state() does not persist learned_targets"
        )


class TestStageClassifierFeatures:
    """Verify the ML classifier training pipeline uses
    the same features the controller collects."""

    def test_training_data_includes_resp_rate(self):
        """Training data collection must include resp_rate_pct
        when respiratory rate data is available."""
        src = _read_controller_source()
        # Find the training data append block
        assert "resp_rate_pct" in src, (
            "Controller does not log resp_rate_pct in "
            "stage_training_data. The ML classifier cannot "
            "learn from respiratory rate data."
        )

    def test_trainer_supports_resp_rate(self):
        """train_stage_classifier.py must support
        resp_rate_pct as a feature."""
        trainer_src = TRAINER_PATH.read_text()
        assert "resp_rate_pct" in trainer_src, (
            "train_stage_classifier.py does not handle "
            "resp_rate_pct. Even if the controller logs it, "
            "the trainer would ignore it."
        )

    def test_classifier_fallback_uses_resp_rate(self):
        """The heuristic fallback in _estimate_stage_from_hr
        should use resp_rate when available."""
        src = _read_controller_source()
        estimate_fn = _extract_method(src, "_estimate_stage_from_hr")
        assert "resp_rate" in estimate_fn, (
            "_estimate_stage_from_hr() ignores respiratory rate "
            "even in heuristic fallback mode"
        )


class TestHealthCheck:
    """Verify the controller has self-diagnostic capability."""

    def test_health_check_exists(self):
        """Controller must have a _health_check method."""
        src = _read_controller_source()
        assert "def _health_check" in src, (
            "No _health_check method. The controller cannot "
            "detect its own failures."
        )

    def test_health_check_called_on_init(self):
        """_health_check must be invoked during initialize()."""
        src = _read_controller_source()
        init_fn = _extract_method(src, "initialize")
        assert "_health_check" in init_fn, (
            "_health_check is not called during initialize(). "
            "Startup issues will go undetected."
        )

    def test_health_check_validates_entities(self):
        """_health_check must verify HEALTH_ENTITIES are reachable."""
        src = _read_controller_source()
        hc_fn = _extract_method(src, "_health_check")
        assert "HEALTH_ENTITIES" in hc_fn, (
            "_health_check does not validate HEALTH_ENTITIES. "
            "Missing sensors will go undetected."
        )

    def test_health_check_validates_learning(self):
        """_health_check must report on learning state."""
        src = _read_controller_source()
        hc_fn = _extract_method(src, "_health_check")
        assert "learned_targets" in hc_fn or "targets_default" in hc_fn, (
            "_health_check does not check if learning has occurred"
        )


class TestControlLoopIntegrity:
    """Verify control loop structural invariants."""

    def test_no_heating_guard(self):
        """Controller must never send positive (heating) settings."""
        src = _read_controller_source()
        assert "new_setting > 0" in src or "min(0, new_setting)" in src, (
            "No heating guard found. Controller could send "
            "positive values and heat the bed unexpectedly."
        )

    def test_continuous_learning_exists(self):
        """Continuous learning rate must be defined and used."""
        src = _read_controller_source()
        assert "CONTINUOUS_LEARN_RATE" in src, (
            "CONTINUOUS_LEARN_RATE not found"
        )
        # Verify it's actually used in the control loop
        loop_fn = _extract_method(src, "_control_loop_inner")
        assert "CONTINUOUS_LEARN_RATE" in loop_fn, (
            "CONTINUOUS_LEARN_RATE defined but not used in control loop"
        )

    def test_save_called_periodically(self):
        """State must be saved periodically during the control loop."""
        src = _read_controller_source()
        loop_fn = _extract_method(src, "_control_loop_inner")
        assert "_save_state" in loop_fn, (
            "_save_state not called in control loop — "
            "state will only be saved at wake"
        )

    def test_control_loop_has_crash_handler(self):
        """Control loop must have top-level exception handling."""
        src = _read_controller_source()
        loop_fn = _extract_method(src, "_control_loop")
        assert "except" in loop_fn and "_save_state" in loop_fn, (
            "_control_loop must catch exceptions and save state on crash"
        )


class TestFStringFormatSafety:
    """F-strings with format specifiers must not use conditional
    expressions inside the format spec (e.g., {x:.1f if x else '?'}).
    Python parses the entire thing after : as a format spec, causing
    ValueError at runtime. This test catches that class of bug."""

    def test_no_conditional_format_specifiers(self):
        """No f-string should have 'if...else' inside a format spec."""
        src = _read_controller_source()
        # Pattern: {variable_name:FORMAT_SPEC if ... else ...}
        # The colon starts a format spec, and 'if' inside it is invalid
        bad_pattern = re.compile(
            r'\{[^}]*:\.[0-9]+[a-z]\s+if\s+.*?else\s+.*?\}')
        matches = bad_pattern.findall(src)
        assert not matches, (
            f"Found conditional expression inside f-string format "
            f"specifier (will crash at runtime): {matches}"
        )

    def test_all_log_fstrings_compile(self):
        """Every self.log() f-string must compile without SyntaxError."""
        src = _read_controller_source()
        # The file must parse cleanly as Python
        try:
            ast.parse(src)
        except SyntaxError as e:
            raise AssertionError(
                f"Controller has syntax error: {e}"
            )

    def test_format_specifiers_are_valid(self):
        """All :.Nf format specifiers must apply to actual numeric
        expressions, not conditional ternaries."""
        src = _read_controller_source()
        # Find all f-string expressions with format specs like :.1f, :.0f
        # Pattern: {EXPR:.Nf} where EXPR should not contain 'if' or 'else'
        fspec_pattern = re.compile(
            r'\{([^}:]+):\.[0-9]+f\}')
        for match in fspec_pattern.finditer(src):
            expr = match.group(1)
            assert ' if ' not in expr and ' else ' not in expr, (
                f"Format specifier applied to ternary expression "
                f"(will crash): {match.group(0)}"
            )


# ── Helpers ──────────────────────────────────────────────────────

def _extract_method(source: str, method_name: str) -> str:
    """Extract the source code of a method from a class."""
    pattern = rf'(    def {method_name}\(.*?)(?=\n    def |\nclass |\Z)'
    match = re.search(pattern, source, re.DOTALL)
    if match:
        return match.group(1)
    return ""
