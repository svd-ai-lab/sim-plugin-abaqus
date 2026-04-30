"""Tier 1 protocol-compliance tests for the Abaqus driver."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sim.driver import SolverInstall

from sim_plugin_abaqus import AbaqusDriver

FIXTURES = Path(__file__).parent.parent / "fixtures"


class TestDetect:
    def setup_method(self):
        self.driver = AbaqusDriver()

    def test_detect_good_py_script(self):
        """Python script with Abaqus import -> True."""
        assert self.driver.detect(FIXTURES / "abaqus_good.py") is True

    def test_detect_good_inp_file(self):
        """Abaqus .inp input deck -> True."""
        assert self.driver.detect(FIXTURES / "abaqus_inp_good.inp") is True

    def test_detect_unrelated_script(self):
        """Script without Abaqus import -> False."""
        assert self.driver.detect(FIXTURES / "not_simulation.py") is False

    def test_detect_no_import_py(self):
        """Python without Abaqus import -> False."""
        assert self.driver.detect(FIXTURES / "abaqus_no_import.py") is False

    def test_detect_missing_file(self):
        """Non-existent path -> False (no exception)."""
        assert self.driver.detect(Path("/does/not/exist.py")) is False

    def test_detect_syntax_error_still_detects(self):
        """Regex-based detection works even on broken syntax."""
        assert self.driver.detect(FIXTURES / "abaqus_syntax_error.py") is True


class TestLint:
    def setup_method(self):
        self.driver = AbaqusDriver()

    def test_lint_good_py(self):
        result = self.driver.lint(FIXTURES / "abaqus_good.py")
        assert result.ok is True
        assert len([d for d in result.diagnostics if d.level == "error"]) == 0

    def test_lint_good_inp(self):
        result = self.driver.lint(FIXTURES / "abaqus_inp_good.inp")
        assert result.ok is True

    def test_lint_bad_syntax(self):
        result = self.driver.lint(FIXTURES / "abaqus_syntax_error.py")
        assert result.ok is False
        assert any(
            d.level == "error" and "syntax" in d.message.lower()
            for d in result.diagnostics
        )

    def test_lint_missing_import(self):
        result = self.driver.lint(FIXTURES / "abaqus_no_import.py")
        assert result.ok is False
        assert any(
            "import" in d.message.lower()
            for d in result.diagnostics
            if d.level == "error"
        )

    def test_lint_no_step_is_warning(self):
        """Input deck without *STEP -> warning, not error."""
        result = self.driver.lint(FIXTURES / "abaqus_no_step.inp")
        assert result.ok is True
        assert any(d.level == "warning" for d in result.diagnostics)


class TestConnect:
    def test_connect_not_installed(self, monkeypatch):
        """When abaqus not found -> status='not_installed'."""
        driver = AbaqusDriver()
        monkeypatch.setattr(driver, "detect_installed", lambda: [])
        info = driver.connect()
        assert info.status == "not_installed"
        assert "SIM_ABAQUS_COMMAND" in info.message

    def test_connect_found(self, monkeypatch):
        """When abaqus found -> status='ok', version populated."""
        from sim.driver import SolverInstall

        driver = AbaqusDriver()
        monkeypatch.setattr(
            driver,
            "detect_installed",
            lambda: [
                SolverInstall(
                    name="abaqus",
                    version="2026",
                    path="C:/SIMULIA",
                    source="test",
                )
            ],
        )
        info = driver.connect()
        assert info.status == "ok"
        assert info.version is not None

    def test_explicit_launcher_env_path(self, monkeypatch, tmp_path):
        """SIM_ABAQUS_COMMAND points at an exact launcher outside defaults."""
        import sim_plugin_abaqus.driver as drv

        bat = tmp_path / "custom_abaqus.bat"
        bat.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [])
        monkeypatch.setenv("SIM_ABAQUS_COMMAND", str(bat))

        installs = AbaqusDriver().detect_installed()

        assert len(installs) == 1
        assert installs[0].source == "env:SIM_ABAQUS_COMMAND"
        assert installs[0].extra["bat"] == str(bat)

    def test_explicit_launcher_env_directory_prefers_versioned_bat(self, monkeypatch, tmp_path):
        """ABAQUS_BAT_PATH can point at a Commands directory."""
        import sim_plugin_abaqus.driver as drv

        (tmp_path / "abaqus.bat").write_text("@echo off\n", encoding="utf-8")
        bat = tmp_path / "abq2026.bat"
        bat.write_text("@echo off\n", encoding="utf-8")
        monkeypatch.setattr(drv, "_INSTALL_FINDERS", [])
        monkeypatch.setenv("ABAQUS_BAT_PATH", str(tmp_path))

        installs = AbaqusDriver().detect_installed()

        assert installs[0].version == "2026"
        assert installs[0].extra["bat"] == str(bat)


class TestParseOutput:
    def setup_method(self):
        self.driver = AbaqusDriver()

    def test_last_json_line(self):
        stdout = 'Abaqus JOB log output\n{"displacement": 0.0123}\n'
        result = self.driver.parse_output(stdout)
        assert result == {"displacement": 0.0123}

    def test_no_json(self):
        result = self.driver.parse_output("just plain solver output\n")
        assert result == {}

    def test_multiple_json_takes_last(self):
        stdout = '{"a": 1}\nsome log\n{"b": 2}\n'
        result = self.driver.parse_output(stdout)
        assert result == {"b": 2}

    def test_invalid_json_skipped(self):
        stdout = '{broken\n{"valid": true}\n'
        result = self.driver.parse_output(stdout)
        assert result == {"valid": True}


class TestRunFile:
    def test_raises_when_not_installed(self, monkeypatch):
        driver = AbaqusDriver()
        monkeypatch.setattr(driver, "detect_installed", lambda: [])
        with pytest.raises(RuntimeError, match="(?i)abaqus"):
            driver.run_file(FIXTURES / "abaqus_good.py")

    def test_run_file_attaches_artifacts_and_diagnostics(self, monkeypatch, tmp_path):
        inp = tmp_path / "model.inp"
        inp.write_text("*HEADING\n*STEP\n*STATIC\n*END STEP\n", encoding="utf-8")
        (tmp_path / "model.msg").write_text("***WARNING: check mesh quality\n", encoding="utf-8")
        (tmp_path / "model.odb").write_bytes(b"odb")

        driver = AbaqusDriver()
        monkeypatch.setattr(
            driver,
            "detect_installed",
            lambda: [
                SolverInstall(
                    name="abaqus",
                    version="2026",
                    path="C:/SIMULIA/Commands",
                    source="test",
                    extra={"bat": "abaqus"},
                )
            ],
        )
        monkeypatch.setattr(
            "sim_plugin_abaqus.driver.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
        )

        result = driver.run_file(inp)

        assert result.ok is True
        assert any(a["kind"] == "odb" for a in result.artifacts)
        assert any(d["level"] == "warning" for d in result.diagnostics)


class TestAuthoringSession:
    def _install(self):
        return SolverInstall(
            name="abaqus",
            version="2026",
            path="C:/SIMULIA/Commands",
            source="test",
            extra={"bat": "abaqus"},
        )

    def test_launch_creates_file_backed_session(self, monkeypatch, tmp_path):
        driver = AbaqusDriver()
        monkeypatch.setattr(driver, "detect_installed", lambda: [self._install()])

        info = driver.launch(mode="cae", ui_mode="no_gui", workspace=str(tmp_path))
        summary = driver.query("session.summary")

        assert info["ok"] is True
        assert info["persistence"] == "file-backed-cae"
        assert summary["connected"] is True
        assert summary["workspace"] == str(tmp_path)
        assert summary["cae_path"].endswith("session.cae")

    def test_run_executes_wrapper_and_updates_inspection(self, monkeypatch, tmp_path):
        driver = AbaqusDriver()
        monkeypatch.setattr(driver, "detect_installed", lambda: [self._install()])
        driver.launch(workspace=str(tmp_path))

        def fake_run(wrapper_path):
            result_path = wrapper_path.parent / "result_0001.json"
            result_path.write_text(
                json.dumps({
                    "ok": True,
                    "result": {"created": "beam"},
                    "model_summary": {
                        "models": {
                            "Model-1": {
                                "parts": ["Beam"],
                                "materials": ["Steel"],
                                "sections": [],
                                "steps": ["Load"],
                                "loads": ["TipLoad"],
                                "boundary_conditions": ["Fixed"],
                                "instances": ["Beam-1"],
                            }
                        },
                        "jobs": ["BeamJob"],
                    },
                }),
                encoding="utf-8",
            )
            (wrapper_path.parent / "session.cae").write_bytes(b"cae")
            return subprocess.CompletedProcess(["abaqus"], 0, stdout="done", stderr="")

        monkeypatch.setattr(driver, "_run_cae_wrapper", fake_run)

        result = driver.run("_sim_result = {'created': 'beam'}", label="build")
        model_summary = driver.query("cae.model_summary")
        files = driver.query("workdir.files")

        assert result["ok"] is True
        assert result["result"] == {"created": "beam"}
        assert model_summary["available"] is True
        assert model_summary["model_summary"]["models"]["Model-1"]["parts"] == ["Beam"]
        assert any(f["kind"] == "cae" for f in files["files"])

    def test_run_without_launch_is_readable_failure(self):
        result = AbaqusDriver().run("print('hello')")

        assert result["ok"] is False
        assert result["error_code"] == "session_not_connected"
