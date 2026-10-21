#!/usr/bin/python3 -I
"""Root-owned runner start hook; reject other workflows before executing job steps."""
import os
import sys


def allowed(environment):
    return environment.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "workflow_run"} and all(
        environment.get(key) == value for key, value in {
            "GITHUB_REPOSITORY": "sginfoocio/abc",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF":
                "sginfoocio/abc/.github/workflows/deploy-cloud-production.yml@refs/heads/main",
            "GITHUB_JOB": "deploy-production",
        }.items()
    )


if __name__ == "__main__":
    if not allowed(os.environ):
        print("Cloud runner rejects this workflow/job", file=sys.stderr)
        sys.exit(1)
