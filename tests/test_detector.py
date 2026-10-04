"""Tests for profile and stack detection logic.

Tests path-first detection, content-second Work-Infra detection,
stack marker detection, and terraform depth guard.
"""

from pathlib import Path
from unittest.mock import patch

from init_permissions.detector import detect_profile, detect_stacks


def test_own_path_detection(tmp_path: Path) -> None:
    """Paths containing /Developer/Own/ always map to 'own'."""
    fake_path = tmp_path / "Developer" / "Own" / "my-project"
    fake_path.mkdir(parents=True)
    with patch("init_permissions.detector.Path.resolve", return_value=fake_path):
        result = detect_profile(fake_path)
    assert result == "own"


def test_work_infra_detection_with_tf_files(tmp_path: Path) -> None:
    """Work/ path with .tf files at root level returns 'work-infra'."""
    # Create a fake path that resolves to a Work path
    work_path = tmp_path / "Developer" / "Work" / "infra-repo"
    work_path.mkdir(parents=True)
    (work_path / "main.tf").write_text('resource "aws_s3_bucket" "test" {}')

    with patch("init_permissions.detector.Path.resolve", return_value=work_path):
        result = detect_profile(work_path)
    assert result == "work-infra"


def test_work_app_detection_without_tf(tmp_path: Path) -> None:
    """Work/ path without .tf files returns 'work-app'."""
    work_path = tmp_path / "Developer" / "Work" / "app-repo"
    work_path.mkdir(parents=True)
    (work_path / "package.json").write_text('{"name": "app"}')

    with patch("init_permissions.detector.Path.resolve", return_value=work_path):
        result = detect_profile(work_path)
    assert result == "work-app"


def test_fallback_outside_developer(tmp_path: Path) -> None:
    """Paths outside /Developer/ fall back to 'own'."""
    other_path = tmp_path / "random" / "path" / "repo"
    other_path.mkdir(parents=True)

    with patch("init_permissions.detector.Path.resolve", return_value=other_path):
        result = detect_profile(other_path)
    assert result == "own"


def test_detect_stacks_node(tmp_path: Path) -> None:
    """package.json presence triggers 'node' stack detection."""
    (tmp_path / "package.json").write_text('{"name": "my-app"}')
    stacks = detect_stacks(tmp_path)
    assert "node" in stacks


def test_detect_stacks_python(tmp_path: Path) -> None:
    """pyproject.toml presence triggers 'python' stack detection."""
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "app"')
    stacks = detect_stacks(tmp_path)
    assert "python" in stacks


def test_detect_stacks_multiple(tmp_path: Path) -> None:
    """Multiple marker files triggers multiple stack detections."""
    (tmp_path / "package.json").write_text('{"name": "app"}')
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.21")
    stacks = detect_stacks(tmp_path)
    assert "node" in stacks
    assert "go" in stacks


def test_detect_stacks_none(tmp_path: Path) -> None:
    """Empty directory yields empty stacks list."""
    stacks = detect_stacks(tmp_path)
    assert stacks == []


def test_tf_depth_limit(tmp_path: Path) -> None:
    """Terraform files at depth 5+ should NOT trigger work-infra detection.

    The detector uses depth <= 4 (parts count from repo root).
    A file at depth 5 (5 path components) is outside the limit.
    """
    work_path = tmp_path / "Developer" / "Work" / "deep-repo"
    work_path.mkdir(parents=True)

    # Create a .tf file at depth 5: a/b/c/d/e/deep.tf (5 parts)
    deep_dir = work_path / "a" / "b" / "c" / "d" / "e"
    deep_dir.mkdir(parents=True)
    (deep_dir / "deep.tf").write_text('resource "fake" "test" {}')

    with patch("init_permissions.detector.Path.resolve", return_value=work_path):
        result = detect_profile(work_path)
    # Deep .tf files should not trigger infra detection — falls back to work-app
    assert result == "work-app"


def test_tf_within_depth_limit(tmp_path: Path) -> None:
    """Terraform files at depth <= 4 DO trigger work-infra detection."""
    work_path = tmp_path / "Developer" / "Work" / "infra-repo"
    work_path.mkdir(parents=True)

    # Create a .tf file at depth 3: envs/prod/main.tf (3 parts)
    nested_dir = work_path / "envs" / "prod"
    nested_dir.mkdir(parents=True)
    (nested_dir / "main.tf").write_text('resource "aws_vpc" "main" {}')

    with patch("init_permissions.detector.Path.resolve", return_value=work_path):
        result = detect_profile(work_path)
    assert result == "work-infra"
