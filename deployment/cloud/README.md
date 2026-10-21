# Cloud production: approved, app-only releases

## Automated release pipeline (supersedes per-release policy edits)

Every release no longer needs a root edit on `s2026`. Responsibilities are split:

| Concern | Where it lives | Who can change it |
|---|---|---|
| Application code | `main` (PR, branch protection, independent review) | Developers |
| Authorization policy (static: repository, runner, reviewers, image repository, source file names, pinned deploy-workflow blob) | `/etc/cloud-production/release-authority.json`, template `release-authority.json` | Administrator, only when the policy itself changes |
| Artifact identity (image digest + build run/attempt) | Commit status `cloud-production/published-image` written by `deploy.yml` after build, verification and push | GitHub Actions on `main` only |
| Privileged controller | `/usr/local/sbin/cloud-production-deploy` (root) | Administrator |
| Production approval | GitHub environment `production` (required reviewers) | Authorized reviewers |

Flow: merge to `main` -> `deploy.yml` (tests, build, push to GHCR, identity
checks, status) -> `deploy-cloud-production.yml` (only when repository variable
`CLOUD_AUTO_DEPLOY` is `true`; otherwise manual `workflow_dispatch`) -> hosted
preflight -> `production` approval -> dedicated runner -> root controller.

The runner sends only `{run_id, attempt}`. The controller then derives and
verifies everything itself, using the public GitHub API over verified TLS:

1. The run is this repository's `deploy-cloud-production.yml`, on `main`,
   triggered by `workflow_dispatch` or `workflow_run`, in progress, on the
   `cloud-production-s2026` runner, approved in `production` by an authorized
   reviewer, and the environment still has required reviewers.
2. Its commit is the *current head of `main`* and the workflow file at that
   commit has exactly the blob SHA pinned in the root authority.
3. The commit has a successful `cloud-production/published-image` status created
   by `github-actions[bot]`, whose description is exactly
   `ghcr.io/sginfoocio/abc@sha256:<64 hex> actions-<run>-<attempt>` and whose URL
   points at that run. The referenced run must be a successful `deploy.yml` push
   run on `main` for the same commit.
4. `previous_commit` is read from the running `abcd-control` revision label and
   must be an ancestor of the commit (forward update only); same-commit replays
   are rejected.
5. SHA-256 of `app_enhanced.py`, `app_exports.py` and
   `transform_luxottica_masterdata.py` are computed from the commit and compared
   with the pulled image (label, build identity and synthetic Luxottica
   regression) before any change.
6. Only `abcd-app` is replaced; health, other-service identity and logs are
   verified; any failure restores the retained `cloud-production:rollback-<previous>`
   image automatically.

No image, tag, path or command comes from workflow inputs. Automatic triggering
is inert until `CLOUD_AUTO_DEPLOY=true` is set by a repository administrator;
unset it to stop automation immediately.

### One-time administrative installation (replaces the per-release step)

From a root-only copy of a reviewed, merged `main` revision (never a
runner-writable checkout), install the new controller, runner gate and static
authority, then verify owner/mode. Do not restart services or the runner.

```sh
SOURCE=/root/cloud-deploy-review/deployment/cloud
install -o root -g root -m 0755 "$SOURCE/cloud_production_deploy.py" /usr/local/sbin/cloud-production-deploy
install -o root -g root -m 0644 "$SOURCE/runner_job_gate.py" /usr/local/libexec/cloud-runner-job-gate.py
install -o root -g root -m 0600 "$SOURCE/release-authority.json" /etc/cloud-production/release-authority.json
```

Then, as repository admin: confirm the `production` environment requires
reviewers (enable "prevent self-review" if a second reviewer exists), confirm
branch protection on `main`, and only when ready set `CLOUD_AUTO_DEPLOY=true`.
The old `/etc/cloud-production/approved-release.json` is no longer read and may
be archived. If `deploy-cloud-production.yml` is ever edited, its new blob SHA
(`git rev-parse HEAD:.github/workflows/deploy-cloud-production.yml`) must be
re-installed in the authority; until then deployments fail closed.

### Rollback

Automatic: the controller restores the retained rollback image on any failure.
Manual: stop automation (`CLOUD_AUTO_DEPLOY` unset), then an administrator
re-tags/pins `cloud-production:rollback-<previous_commit>` in
`/etc/cloud-production/compose.private.json` and runs the documented app-only
Compose command. Rolling back the *pipeline* itself: re-install the previous
controller and authority files and leave automation disabled.

### Residual risks

- A commit status can be written by any workflow with `statuses: write`; the
  controller therefore treats it only as a hint and re-verifies run, labels,
  build identity and source hashes. Keep `deploy.yml` changes under review.
- Trust in `deploy.yml` (what gets built) rests on branch protection and review.
- The GitHub API is queried unauthenticated; rate limiting fails closed.
- With a single reviewer, that person approves their own production deployments;
  add a second reviewer and enable prevent-self-review to get independent approval.

> The sections below are historical runbooks written for the earlier per-release
> policy (`approved-release.json`). Where they conflict with this section, this
> section applies.

## Current audit status (2026-10-21)

The latest reviewed `main` is `da9dc9a833569f45f43cd85e58f3164a3845b2ef`.
Its post-merge workflow run `39574406488` completed successfully for both
`tests` and `build-and-push`; this proves CI and GHCR publication, not production
deployment. The checked-in `approved-release.json` still says `approved` for
commit `64276b40d2bfa42bc7ff349a4390dadd62638eb0`, image digest
`sha256:04ac4836361e59a59fb2f354f1dc05ce5aa71bbc427a0f603a017d87f29a01ec`,
and publication run `39065951026`. Do not infer that this policy is installed
on the host or that its image is running.

The `s2026` hostname did not resolve during the read-only audit on 2026-10-21.
Consequently, live containers, effective Compose configuration, NAS mounts,
alert scheduling, disk space, recent logs and the installed root policy remain
unverified. GitHub environment protection settings also require independent
verification before any release dispatch. Do not use the historical host,
commit, or digest examples below as current production facts.

The `deploy.yml` workflow runs tests and publishes to GHCR; it does not deploy.
Production is a separate manual `workflow_dispatch` using the `production`
environment and the dedicated runner. The hosted preflight and root helper
validate the pinned publication and deployment request; the helper changes
only the app image, waits for health, and rolls back on a failed transition.
No SSH deployment secrets are used by this flow. Never dispatch until the
installed policy, environment protection, live production state, candidate
digest and rollback target have all been checked and explicitly approved.

## Scope and diagnosis

The following incident is historical context for the earlier SSH deployment
workflow, not a statement about the current production version:

Run 159 (`38772528737`) published commit
`99a9f60eb6fbe11cc231b5626556e5db9b5166e9`; tests/build succeeded but
`Validate Secrets` and `Deploy to Server` were **skipped** because
`CLOUD_AUTO_DEPLOY` was absent. Green publication did not mean deployment.

The existing Mustotal runner
`actions.runner.sginfoocio-Mustotal_3.0.s2026.service` is unrelated and must not
be modified, stopped, reconfigured or used for Cloud.

The publication workflow remains responsible for tests and GHCR. Its obsolete
SSH deployment has been removed: a push now explicitly reports **publication
only**. `Deploy Cloud Production` is a separate `workflow_dispatch` on `main`,
with a global concurrency group, protected environment `production`, and
bounded execution. It initially has **no user inputs**.

The deploy job uses a dedicated local runner on s2026. No inbound public SSH,
root SSH, copied legacy SSH secret or Docker group membership is needed.
Administrative preparation can use the existing host access; Actions itself
uses the local restricted sudo command.

## Trust boundaries

* Runner identity: `cloud-deploy`; name `cloud-production-s2026`; label
  `cloud-production`; no sudo/docker group, no access to private config, NAS or
  root files.
* Runner start hook rejects other repositories/events/branches/workflows/jobs.
  This is a **public repository**: do not use this runner for PRs, builds or
  general commands. Require administrative approval for **all fork workflows**.
  If organization runner groups restricted to selected workflows are available,
  restrict the group to this workflow on main as an additional boundary.
  Isolate the account/network; a persistent runner is not a general sandbox.
* Runner executes no checkout, downloaded action, arbitrary shell input, image
  argument, or privileged path. Its only sudo permission is the root-owned
  executable **with no command-line arguments**.
* The executable accepts bounded JSON stdin containing only numeric run ID and
  attempt. Independently verifies GitHub API data: reviewed controller SHA,
  dispatch on main, correct active job/runner, explicit authorized production
  approval, protection still enabled, and successful main publication.
* Root policy pins repository, current/new commits, GHCR digest, publication
  run/attempt and exact application source hashes. OCI labels alone are not
  treated as provenance. Candidate and deployed container verify build metadata
  **and the pinned source bytes**.
* The in-container regression uses a small, versioned synthetic fixture with a
  pinned checksum. It tests both collection mappings, EAN preservation,
  `8056266003066`, and preview/XLSX/CSV consistency without customer data.
* Root configuration is an independently reviewed, normalized Compose snapshot.
  The existing private config is owned under application-managed directories;
  it cannot safely be a mutable instruction source for a privileged runner.
  Do **not** chmod/chown it or its parents. Keep it unchanged.
* Every privileged auxiliary file and ancestor is root-owned, non-symlink and
  not group/world-writable. Python uses `-I`, an explicit environment and
  absolute executables; no runner PYTHONPATH, PATH, cwd or shell interpolation.
* Root systemd worker survives runner/job cancellation. Separate host locks
  prevent concurrent submissions/transactions. Do not cancel/kill the worker
  during rollback. It has 20-minute start and 5-minute termination bounds.

## Files to review BEFORE any privileged installation

| Repository source | Installed destination |
|---|---|
| `cloud_production_deploy.py` | `/usr/local/sbin/cloud-production-deploy` root:root 0755 |
| `runner_job_gate.sh` | `/usr/local/libexec/cloud-runner-job-gate.sh` root:root 0755 |
| `runner_job_gate.py` | `/usr/local/libexec/cloud-runner-job-gate.py` root:root 0644 |
| `validate_luxottica.py` | `/etc/cloud-production/validate_luxottica.py` root:root 0600 |
| `verify_compose_transition.py` | `/usr/local/libexec/cloud-verify-compose-transition.py` root:root 0700; administrative read-only check, no sudoers grant |
| `release-authority.json` | `/etc/cloud-production/release-authority.json` root:root 0600 (static; replaces per-release `approved-release.json`) |
| `cloud-production.sudoers` | `/etc/sudoers.d/cloud-production` root:root 0440 |
| `cloud-production-deploy.service` | `/etc/systemd/system/cloud-production-deploy.service` root:root 0644 |
| `cloud-image-services.override.conf` | `/etc/systemd/system/cloud-image-services.service.d/cloud-production.conf` root:root 0644 |

Additional root-only files/directories:

* `/etc/cloud-production` root:root 0700.
* `/etc/cloud-production/compose.private.json` root:root 0600: reviewed effective
  snapshot, all existing service settings preserved, no external include,
  extends, env_file, build, configs/secrets or relative bind paths.
* `/var/lib/cloud-production-deploy` root:root 0700: locks, request, result,
  per-run configuration backups, container identities and receipts.

The systemd drop-in makes future boot use the controlled configuration, avoiding
an unnoticed return to the old image. Installing/reloading it must **not restart**
`cloud-image-services.service` (which owns all Cloud services).

## Archived administrative installation proposal — do not execute

> **Historical runbook, not an approved procedure.** The commands below assume
> a production baseline at `99a9f60...` and a `baseline-only` policy, which
> conflict with the currently checked-in policy and have not been verified on
> `s2026`. They also assume installed host files and services whose current
> state could not be inspected. Do not execute these commands or treat their
> assumptions as production facts. Replace this section only after a
> read-only host audit, review of the installed policy and configuration, and
> explicit administrative authorization.

The historical proposal is retained below for audit context only. It is not a
request to install or change any host configuration.

### Updating the controller already installed on s2026

This repository already has the dedicated runner and systemd units installed.
For this controller update, do **not** repeat runner registration, install or
start its service, change sudoers, replace the Compose snapshot, reload units,
start the deploy worker, dispatch a workflow or restart any application service.
The runner's present online/offline state has not been verified; check it
read-only before any later dispatch, and do not activate it as part of this
update.

After the PR is reviewed and merged, stage the exact reviewed `main` commit
under a root-only directory outside the runner workspace. Confirm production
still reports healthy at `99a9f60...` and there is no active production
workflow. Install only the root helper, its synthetic regression validator and
the baseline-only policy. This example deliberately leaves release dispatch
disabled:

The controller's GitHub API client requires the verified system CA bundle at
`/etc/ssl/certs/ca-certificates.crt`. Confirm that file is present and valid
before a later authorized installation. TLS certificate and hostname
verification remain enabled; do not replace this trust store with an insecure
context or bypass HTTPS verification.

```sh
umask 077
SOURCE=/root/cloud-deploy-review/deployment/cloud
CONTROLLER_SHA=REPLACE_WITH_REVIEWED_40_CHARACTER_MAIN_COMMIT
python3 - "$SOURCE/approved-release.json" "$CONTROLLER_SHA" <<'PY'
import json, os, pathlib, re, sys

source, controller = pathlib.Path(sys.argv[1]), sys.argv[2]
assert re.fullmatch(r"[0-9a-f]{40}", controller)
policy = json.loads(source.read_text(encoding="utf-8"))
assert policy["release_status"] == "baseline-only"
assert policy["commit"] == policy["previous_commit"] == "99a9f60eb6fbe11cc231b5626556e5db9b5166e9"
policy["controller_commit"] = controller
target = pathlib.Path("/etc/cloud-production/approved-release.json.new")
target.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
os.chown(target, 0, 0)
os.chmod(target, 0o600)
os.replace(target, "/etc/cloud-production/approved-release.json")
PY
install -o root -g root -m 0755 "$SOURCE/cloud_production_deploy.py" /usr/local/sbin/cloud-production-deploy
install -o root -g root -m 0600 "$SOURCE/validate_luxottica.py" /etc/cloud-production/validate_luxottica.py
```

Review the staged source and file hashes before installation. The policy and
controller SHA must refer to the reviewed merged revision. No `daemon-reload`
is needed for these two Python files; verify their owner/mode and leave the
runner service and all application services untouched. `release_status` must
remain `baseline-only` until a *different, forward* main publication has been
reviewed and its digest/build/source hashes installed in both policies. Before
that future dispatch, inspect (do not start or restart) the dedicated runner
unit and GitHub runner status; separately authorize any activation if it is
offline.

1. Stage an inspected copy of this directory under a root-only review directory.
   Set `SOURCE` to it and `CONTROLLER_SHA` to the exact reviewed commit that
   contains the new workflow. Verify repository/source SHA outside the runner.
   Never install from a runner-writable checkout.

   ```sh
   # In an existing root administrative session:
   umask 077
   SOURCE=/root/cloud-deploy-review/deployment/cloud
   CONTROLLER_SHA=REPLACE_WITH_REVIEWED_40_CHARACTER_COMMIT
   install -d -o root -g root -m 0700 /etc/cloud-production /var/lib/cloud-production-deploy
   install -o root -g root -m 0755 "$SOURCE/cloud_production_deploy.py" /usr/local/sbin/cloud-production-deploy
   install -o root -g root -m 0755 "$SOURCE/runner_job_gate.sh" /usr/local/libexec/cloud-runner-job-gate.sh
   install -o root -g root -m 0644 "$SOURCE/runner_job_gate.py" /usr/local/libexec/cloud-runner-job-gate.py
   install -o root -g root -m 0600 "$SOURCE/validate_luxottica.py" /etc/cloud-production/validate_luxottica.py
   install -o root -g root -m 0700 "$SOURCE/verify_compose_transition.py" /usr/local/libexec/cloud-verify-compose-transition.py
   install -o root -g root -m 0600 "$SOURCE/approved-release.json" /etc/cloud-production/approved-release.json
   python3 - "$CONTROLLER_SHA" <<'PY'
   import json, pathlib, re, sys
   assert re.fullmatch(r"[0-9a-f]{40}", sys.argv[1])
   path = pathlib.Path("/etc/cloud-production/approved-release.json")
   data = json.loads(path.read_text())
   assert data["release_status"] == "baseline-only"
   assert data["commit"] == data["previous_commit"] == "99a9f60eb6fbe11cc231b5626556e5db9b5166e9"
   data["controller_commit"] = sys.argv[1]
   path.write_text(json.dumps(data, indent=2) + "\n")
   PY
   ```

2. As root, capture the current effective Compose configuration privately.
   Do not print it to Actions, chat or logs: it contains credentials.

   ```sh
   docker compose --project-name cloud-nas \
     -f /opt/cloud-image-production/active/compose.private.json \
     config --format json > /etc/cloud-production/compose.private.json
   chmod 0600 /etc/cloud-production/compose.private.json
   ```

   Review this snapshot locally against container inspection and the original.
   All mounts must be absolute and preserve the NAS identity/guard settings,
   UID, capabilities, environment, healthcheck, networks and restart policies.
   If there is a `build` entry, remove only that unused build instruction from
   the **new** snapshot after review; never rebuild on deployment. Other external
   resources cause a hard rejection and require explicit normalization/review.
   No paths may point at runner-writable auxiliary files.
   Review Compose interpolation (`$`/`$$` in credentials) through a second
   private `docker compose ... config --format json` round trip: effective values
   must remain identical. No environment-dependent `.env` is used by the helper.
   Verify app user `1037:100`, project `cloud-nas`, container `abcd-control`,
   and the current image ID/commit `99a9f60eb6fb`.

   **Mandatory read-only transition gate, before installing the drop-in:**

   ```sh
   /usr/bin/python3 -I /usr/local/libexec/cloud-verify-compose-transition.py
   ```

   This compares the complete effective JSON from the original and controlled
   configurations (including paths/mounts, UID/GID, networks, capabilities,
   variables, healthchecks and every other value). Only removal of unused
   `build` instructions is allowed. It also compares Compose's service hashes
   across the changed project directory, and checks them and resolved local
   image IDs against **every existing Cloud container**. It does not call `up`,
   `stop`, `pull`, mount commands or write configuration; no secret values are
   printed. Missing access, drift, a non-running/unhealthy service, duplicate
   service containers or a missing local image cause rejection. Resolve and
   review differences privately; do not bypass this gate or alter live services.
   These checks cannot be reported as passed until the administrator runs them
   against the actual private files.

3. The Luxottica regression fixture is source-controlled, synthetic and
   checksum-pinned in `validate_luxottica.py`. Do not copy a customer workbook
   to the server, Actions, GHCR, NAS or a Docker volume. The in-container test
   uses no network, database or Odoo access.

4. After the equivalence check passes, install unit and drop-in without starting
   a deployment or restarting Cloud:

   ```sh
   systemctl show cloud-image-services.service \
     --property=Requires --property=After --property=BindsTo \
     > /var/lib/cloud-production-deploy/unit-dependencies.before
   install -o root -g root -m 0644 "$SOURCE/cloud-production-deploy.service" /etc/systemd/system/cloud-production-deploy.service
   install -d -o root -g root -m 0755 /etc/systemd/system/cloud-image-services.service.d
   install -o root -g root -m 0644 "$SOURCE/cloud-image-services.override.conf" \
     /etc/systemd/system/cloud-image-services.service.d/cloud-production.conf
   systemd-analyze verify /etc/systemd/system/cloud-production-deploy.service \
     /etc/systemd/system/cloud-image-services.service
   systemctl daemon-reload
   systemctl cat cloud-image-services.service
   systemctl show cloud-image-services.service \
     --property=Requires --property=After --property=BindsTo \
     > /var/lib/cloud-production-deploy/unit-dependencies.after
   cmp /var/lib/cloud-production-deploy/unit-dependencies.before \
     /var/lib/cloud-production-deploy/unit-dependencies.after
   systemctl show cloud-image-services.service \
     --property=Conditions --property=WorkingDirectory --property=ExecStart \
     --property=ExecStop --property=ActiveState --property=SubState
   # Do NOT start/enable the release worker or restart Cloud here.
   ```

   The effective unit must retain its existing `Requires`/`After` for Docker
   and `mnt-cloud\x2dimagenes.mount`, and `BindsTo` for that mount. The drop-in
   deliberately sets **none** of these dependency directives. It clears the old
   `ConditionPathExists` before setting `/etc/cloud-production/compose.private.json`,
   and sets `WorkingDirectory` and both Compose `--project-directory` arguments
   to `/etc/cloud-production`. No condition referencing the old file may remain.
   `ConditionResult` alone describes the last activation, not a re-evaluation
   after daemon-reload; inspect `Conditions` and unit text without restarting.

   Boot/service start uses `--no-recreate --no-build --pull never`: existing
   container IDs are retained, stopped containers can be started, and missing
   containers can be created from existing local images. It does not silently
   reconcile drift or pull/rebuild an image. A service **restart** still invokes
   `ExecStop` and therefore interrupts all Cloud services; do not use restart as
   an installation test. The separate release transaction intentionally does
   **not** use `--no-recreate`, because it must replace `abcd-app` only. It retains
   `--no-deps` and cannot update Kering, monitors, alerts or Mustotal.

5. (First-time setup only; do not repeat for the controller update above.)
   Create a separate runner account. Confirm it has no supplementary docker,
   sudo, users/NAS groups, no login credentials, and cannot read private config:

   ```sh
   useradd --system --create-home --home-dir /var/lib/cloud-actions-runner \
     --shell /usr/sbin/nologin cloud-deploy
   install -d -o cloud-deploy -g cloud-deploy -m 0700 /var/lib/cloud-actions-runner/runner
   id cloud-deploy
   ```

   Download the official runner to a root review directory and verify its
   checksum before unpacking. Confirm version/checksum against
   `gh api repos/sginfoocio/abc/actions/runners/downloads`. Observed distribution:

   ```text
   https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz
   SHA256 70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613
   ```

   Unpack that verified archive into the new runner directory, owned by
   `cloud-deploy`. Obtain a **fresh** repository registration token via
   `gh api --method POST repos/sginfoocio/abc/actions/runners/registration-token`;
   it is short-lived: do not commit, log, paste into chat or reuse Mustotal's token.
   In the new directory only, configure as that user:

   ```sh
   # RUNNER_TOKEN comes from secure administrative input, not this document.
   runuser -u cloud-deploy -- ./config.sh --unattended \
     --url https://github.com/sginfoocio/abc --token "$RUNNER_TOKEN" \
     --name cloud-production-s2026 --labels cloud-production --work _work
   unset RUNNER_TOKEN
   printf '%s\n' 'ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/libexec/cloud-runner-job-gate.sh' >> .env
   # Review the freshly extracted official service installer before invoking.
   ./svc.sh install cloud-deploy
   ./svc.sh start
   ```

   Only the new `actions.runner.sginfoocio-abc.cloud-production-s2026.service`
   may be installed/started. Check its User and hook, then record it as online.
   Do not touch the Mustotal installation. Account setup has no `usermod -aG
   docker`, broad sudo permission, NAS ACL change or production service restart.

6. Validate and install the exact sudoers rule:

   ```sh
   visudo -cf "$SOURCE/cloud-production.sudoers"
   install -o root -g root -m 0440 "$SOURCE/cloud-production.sudoers" /etc/sudoers.d/cloud-production
   visudo -c
   sudo -l -U cloud-deploy
   ```

   Only `/usr/local/sbin/cloud-production-deploy ""` is permitted. The empty
   argument string is significant: omitting it would allow arbitrary arguments.
   In particular `--worker`, `docker`, `systemctl`, shells and arbitrary scripts
   must not be independently authorized.

## Protected environment and dispatch

Configure `production` with required reviewer **sginfoocio** (current repository
administrator), no administrator bypass, and deployment branch policy allowing
only `main`. With the current single administrator, self-review prevention
cannot be used when that same administrator dispatches: an explicit
administrative environment approval is still mandatory and independently checked
by the root helper. Prefer a second administrator and prevent self-review when
available. Never approve automatically or call the approval API from the runner.

The deploy job uses no SSH secrets and has no token permissions. Its hosted
preflight has only `contents: read` and `actions: read`.

After the controller commit passes CI, root installation is reviewed/authorized,
the dedicated runner is online and the environment is protected:

```sh
gh workflow run deploy-cloud-production.yml --repo sginfoocio/abc --ref main
```

An administrator must review the pinned SHA/digest in the hosted preflight
summary and approve `production` in Actions. Do not bypass waiting approval.
The helper will reject missing/stale policy, wrong controller SHA, unsuccessful
publication, unrelated runner/job or missing reviewer approval **before** mutation.
Public GitHub API unavailability/rate limiting fails closed; no private token
is necessary for this public repository.

## Transaction, validation and recovery

Before mutation: check all root paths/config, current healthy commit, expected
Cloud/Mustotal containers; retain the current image under a rollback tag;
save private config/container snapshots; pull **pinned digest**; check label,
build/source identity and the synthetic regression in an isolated candidate
(`--network none`, read-only, UID 1037:100, no host mounts).

Only `services.abcd-app.image` changes in the controlled JSON, with an atomic
write preserving file owner/group/mode. The original private configuration is
never edited. Compose is always:

```sh
/usr/bin/docker compose --project-directory /etc/cloud-production \
  --project-name cloud-nas -f /etc/cloud-production/compose.private.json \
  up -d --no-deps --no-build --pull never --wait --wait-timeout 180 abcd-app
```

Afterward: exact image ID/commit, running/healthy and Streamlit health endpoint;
synthetic regression using the persisted dictionary; both collection mappings,
EAN `8056266003066` as MONTURAS, no EAN loss, matching preview/XLSX/CSV values;
startup logs, and **all** non-target container IDs/images/running/health states
unchanged. Running monitor/Kering proves process state, not a new supplier
transaction: do not trigger supplier downloads, alerts or Mustotal writes.

On transaction failure, restore the backed-up config's settings with only app
image pinned to the retained previous image, run the same app-only Compose,
verify old commit/image/health and other containers. Report failure even if
rollback succeeds. Failure of rollback is explicitly
`failed_manual_intervention_required`; do not claim success. Receipts persist
under `/var/lib/cloud-production-deploy/run-RUN-ATTEMPT/`. Only sanitized
SHA/digest/results/status appear in Actions, never config/raw logs/customer rows.

If Actions is cancelled, the root systemd transaction continues its validation
or rollback. Review `systemctl status cloud-production-deploy.service`,
`/var/lib/cloud-production-deploy/result.json` and live health before dispatching
again. A second invocation cannot overwrite an active transaction. If a second
termination, host crash, Docker daemon failure or power loss interrupts recovery,
root must inspect the durable backup and manually run the same app-only recovery;
no software can guarantee an automatic rollback while the host/daemon is down.
No database migration is performed; no volumes, images or customer files are deleted.

## Future releases

Do not accept an image/command/path from a dispatch input. Before a future
release, publish it through CI and update the reviewed policy with that build's
commit, immutable GHCR digest, run/attempt and exact application source hashes.
Set `previous_commit` only to the production revision confirmed by a separate
read-only host audit; the checked-in policy is not proof of that revision.
The candidate must be a forward descendant, and both hosted preflight and root
helper must validate its publication identity and exact controller revision
before any mutation. A same-commit replay, behind/diverged commit or mismatch
must fail closed.

As of this documentation update, the checked-in policy is still marked
`approved` for commit `64276b40d2bfa42bc7ff349a4390dadd62638eb0`, not the current
`main` commit. Do not edit or install either the repository policy or the
root-installed policy as part of routine validation. Preparing a new candidate
requires a successful publication, verified production baseline, exact source
hashes for `app_enhanced.py`, `app_exports.py` and
`transform_luxottica_masterdata.py`, a reviewed `controller_commit`, and
explicit release authorization. The runner cannot change the root-installed
policy. Install reviewed controller files only from a separately staged,
reviewed source tree; do not install from the runner checkout, dispatch a
deployment, or restart Cloud during installation. Retain rollback images until
a separate authorized retention decision. A newer `main` commit cannot
silently deploy under an older controller authorization.
