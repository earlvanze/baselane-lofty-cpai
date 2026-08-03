from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEEDER = ROOT / "scripts" / "baselane_seed_session.sh"


def test_session_seed_success_is_guarded_by_graphql_auth_proof() -> None:
    source = SEEDER.read_text(encoding="utf-8")

    assert (
        'AUTH_CHECK=(python3 "$SCRIPT_DIR/baselane_cdp_auth_recovery.py" '
        "--graphql-auth-smoke)"
    ) in source
    assert 'if run_python_seed && "${AUTH_CHECK[@]}"; then' in source
    assert 'if "${AUTH_CHECK[@]}"; then' in source
    assert 'python3 "$SCRIPT_DIR/baselane_seed_session_via_cdp.py"' in source


def test_obsolete_route_only_javascript_seeder_is_not_the_entry_point() -> None:
    source = SEEDER.read_text(encoding="utf-8")

    assert 'node "$SCRIPT_DIR/baselane_seed_session_via_cdp.js"' not in source
    assert "Route changes alone are not" in source
