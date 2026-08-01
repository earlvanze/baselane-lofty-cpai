from pathlib import Path

from scripts.baselane_hemlane_live_transactions import (
    default_auth_file,
    openclaw_workspace_root,
)


def test_resolves_openclaw_workspace_from_nested_repository(tmp_path: Path) -> None:
    workspace = tmp_path / ".openclaw" / "workspace"
    repository = workspace / "repos" / "baselane-lofty-cpai"
    repository.mkdir(parents=True)

    assert openclaw_workspace_root(repository) == workspace
    assert default_auth_file(repository) == workspace / ".secrets" / "hemlane_auth.json"


def test_repository_local_auth_file_takes_precedence(tmp_path: Path) -> None:
    workspace = tmp_path / ".openclaw" / "workspace"
    repository = workspace / "repos" / "baselane-lofty-cpai"
    auth_file = repository / ".secrets" / "hemlane_auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.touch()

    assert default_auth_file(repository) == auth_file
