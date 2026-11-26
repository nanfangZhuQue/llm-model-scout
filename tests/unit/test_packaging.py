import tomllib
from pathlib import Path


def test_sdist_uses_explicit_allowlist():
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    included = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert set(included) == {
        "/.gitignore",
        "/src",
        "/tests",
        "/examples",
        "/scripts",
        "/start-web.bat",
        "/start-cli.bat",
        "/README.md",
        "/pyproject.toml",
        "/docs/README.md",
        "/docs/requirements",
        "/docs/design",
        "/docs/user-guide",
        "/docs/development",
        "/docs/archive/README.md",
        "/.github/workflows/tests.yml",
    }
    assert not any("claude" in value or "mcp" in value or "opencode" in value for value in included)
