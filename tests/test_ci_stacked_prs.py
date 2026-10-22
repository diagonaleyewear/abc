from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")


def block(name: str) -> str:
    start = WORKFLOW.index(f"\n  {name}:\n")
    following = [i for i in (WORKFLOW.find("\n  build-and-push:\n", start + 1), WORKFLOW.find("\n  tests:\n", start + 1))
                 if i > 0]
    return WORKFLOW[start:min(following) if following else len(WORKFLOW)]


def trigger(kind: str) -> str:
    start = WORKFLOW.index(f"\n  {kind}:\n")
    end = WORKFLOW.index("\n\n", start)
    section = WORKFLOW[start:end]
    return section[:section.index("\n  pull_request:", 1)] if kind == "push" and "\n  pull_request:" in section else section


def test_pull_requests_run_for_any_base_so_stacked_prs_get_checks():
    section = trigger("pull_request")
    assert "- '**'" in section


def test_push_is_still_limited_to_main_and_master():
    section = trigger("push")
    assert "- main" in section and "- master" in section and "'**'" not in section


def test_tests_job_runs_for_pull_requests_and_is_required_by_the_build():
    assert "if: github.event_name == 'pull_request' || github.event_name == 'push'" in block("tests")
    assert "needs: tests" in block("build-and-push")


def test_publication_and_registry_login_remain_push_only():
    build = block("build-and-push")
    assert "push: ${{ github.event_name == 'push' }}" in build
    login = build[build.index("name: Log in to Container Registry"):]
    assert login.splitlines()[1].strip() == "if: github.event_name == 'push'"
    assert "secrets." not in block("tests")
    assert "pull_request_target" not in WORKFLOW


def test_production_deploy_is_only_triggered_by_a_successful_push_run_on_main():
    production = (ROOT / ".github/workflows/deploy-cloud-production.yml").read_text(encoding="utf-8")
    assert "github.event.workflow_run.event == 'push'" in production
    assert "github.event.workflow_run.head_branch == 'main'" in production
