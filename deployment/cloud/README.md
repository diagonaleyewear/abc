# Cloud production: manual, approved, app-only releases

## Scope and diagnosis

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
  attempt. Independently verifies GitHub API data: the dispatch is on `main`
  and descends from the reviewed controller commit, its workflow file is
  byte-identical to that pinned revision, and its committed release policy
  exactly matches the root-installed policy (apart from `controller_commit`).
  It also verifies the correct active job/runner, explicit authorized
  production approval, protection still enabled, and successful publication.
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
| `approved-release.json` | `/etc/cloud-production/approved-release.json` root:root 0600; add reviewed `controller_commit` |
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

## Administrative installation proposal (not automatic)

These commands are for an administrator **after reviewing/authorizing the
installation and the exact source commit**. They are not a request to run
unreviewed code as root. Execute in an administrative session; do not grant a
general sudo shell to the runner.

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
commit, GHCR digest, run/attempt and exact application source hashes. Keep
`previous_commit` equal to the verified production commit (`99a9f60...` until
production is intentionally updated), set `release_status` to `approved`, and
ensure the target is a descendant of production. A same-commit replay, a
behind/diverged commit, or any mismatch is rejected before mutation. The
checked-in policy is intentionally `baseline-only`: it records the deployed
`99a9f60...` release and cannot dispatch/redeploy it. For each later release,
review the successful `main` build's exact image digest and run/attempt, record
the new commit and SHA256 of `app_enhanced.py`, `app_exports.py` and
`transform_luxottica_masterdata.py`, and set the candidate status to `approved`
in both the reviewed repository policy and root-installed policy. Keep
`controller_commit` pinned to the reviewed main revision that anchors the
trusted deployment workflow. A later release-policy commit is permitted only
as a descendant of that anchor, with the workflow file unchanged; the root
helper also requires the committed policy to exactly match its root-installed
policy, excluding the administrative `controller_commit` field. The hosted
preflight and root helper independently check the publication and forward
application-commit ancestry. The runner cannot change the root-installed policy.
Install the reviewed controller, validator and baseline policy manually as root
from a separately staged, reviewed source tree; add the exact reviewed main
commit as `controller_commit`. Do not enable a candidate in the checked-in
policy until a new successful publication exists. Do not install from the
runner's checkout, start a deployment, or restart Cloud as part of installation.
Retain rollback images until a separate authorized retention decision. A newer
main commit cannot silently deploy under an older controller authorization.
