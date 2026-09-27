"""Project scaffolding: creates the artifact folder structure, config, env
template, a starter testplan for the chosen platform, and a portable
run_tests.sh so anyone can re-run the suite from a terminal.
"""
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.common import (  # noqa: E402
    AGENT_ROOT, PROJECT_DIRS, now_iso, project_dir, save_config, slugify,
)

GITIGNORE = """# QA artifacts that must never be committed
.env.qa
.auth-state.json
screenshots/
logs/
test_results/*.pdf
test_results/*.html
test_results/results-*.json
"""

# NOTE: built with str.replace, not .format() - the body contains ${VAR} literals
# that would be mistaken for format fields.
ENV_TEMPLATE = """# Credentials and secrets for this project's test run.
# Reference these in testplan.json as ${QA_USERNAME} / ${QA_PASSWORD} etc.
# This file is gitignored - never commit real credentials.
QA_USERNAME=
QA_PASSWORD=
# Identify who ran the tests in the report header:
QA_USER=__USER__
QA_USER_EMAIL=__EMAIL__
"""

# Starter plans double as executable documentation of each platform's step DSL.
STARTER_PLANS = {
    "web": {
        "suites": [{
            "id": "F-01",
            "name": "Smoke - application loads",
            "cases": [{
                "id": "TC-WEB-001",
                "description": "Application root loads successfully and renders a title",
                "type": "Functional",
                "priority": "P1",
                "tags": ["smoke"],
                "preconditions": "Target URL is reachable from the test host",
                "expected": "Page responds 2xx/3xx, document title is non-empty, no uncaught console errors",
                "steps": [
                    {"action": "goto", "url": "/"},
                    {"action": "screenshot", "name": "landing"},
                    {"action": "expectTitleNotEmpty"},
                    {"action": "expectNoConsoleErrors"},
                ],
            }],
        }],
    },
    "extension": {
        "suites": [{
            "id": "F-01",
            "name": "Smoke - extension loads",
            "cases": [{
                "id": "TC-EXT-001",
                "description": "Extension is installed, has an id, and its popup renders",
                "type": "Functional",
                "priority": "P1",
                "tags": ["smoke"],
                "preconditions": "Unpacked extension directory contains a valid manifest.json",
                "expected": "Extension id resolves and popup.html renders visible content",
                "steps": [
                    {"action": "expectExtensionLoaded"},
                    {"action": "openPopup"},
                    {"action": "screenshot", "name": "popup"},
                    {"action": "expectNoConsoleErrors"},
                ],
            }],
        }],
    },
    "android": {
        "suites": [{
            "id": "F-01",
            "name": "Smoke - app launches",
            "cases": [{
                "id": "TC-AND-001",
                "description": "App installs, launches, and reaches its first interactive screen",
                "type": "Functional",
                "priority": "P1",
                "tags": ["smoke"],
                "preconditions": "Device/emulator online via adb; APK path or appPackage configured",
                "expected": "App launches without crash and the first screen renders",
                "steps": [
                    {"action": "screenshot", "name": "launch"},
                    {"action": "expectAppRunning"},
                ],
            }],
        }],
    },
}
STARTER_PLANS["ios"] = STARTER_PLANS["android"]

RUN_TESTS_SH = r"""#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Portable test entrypoint for project: {project}   (platform: {platform})
#
#   ./run_tests.sh                  run every test case, then build the report
#   ./run_tests.sh --only TC-001    run specific case ids (comma separated)
#   ./run_tests.sh --tag smoke      run only cases carrying a tag
#   ./run_tests.sh --headed         watch the browser (web/extension)
#   ./run_tests.sh --no-report      skip PDF/HTML generation
#   ./run_tests.sh --doctor         check this machine can run the suite
#
# Configuration lives in .qa-config.json; secrets in .env.qa (gitignored).
# Requires: python3, plus the platform toolchain reported by --doctor.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
AGENT_ROOT="{agent_root}"
PLATFORM="{platform}"

if [[ ! -d "$AGENT_ROOT" ]]; then
  echo "ERROR: QA agent framework not found at $AGENT_ROOT" >&2
  echo "Set AGENT_ROOT in this script to the location of qa-agent/." >&2
  exit 1
fi

# Load .env.qa so credentials referenced as ${{VAR}} in testplan.json resolve.
if [[ -f "$PROJECT_DIR/.env.qa" ]]; then
  set -a; source "$PROJECT_DIR/.env.qa"; set +a
fi

REPORT=1
RUN_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --doctor)    exec python3 "$AGENT_ROOT/core/doctor.py" "$PLATFORM" ;;
    --no-report) REPORT=0 ;;
    *)           RUN_ARGS+=("$arg") ;;
  esac
done

echo "=== QA run: {project} [$PLATFORM] ==="
set +e
python3 "$AGENT_ROOT/qa_run.py" --project-dir "$PROJECT_DIR" ${{RUN_ARGS[@]+"${{RUN_ARGS[@]}}"}}
RUN_EXIT=$?
set -e

if [[ "$REPORT" == "1" ]]; then
  echo "=== Building report ==="
  python3 "$AGENT_ROOT/core/report.py" --project-dir "$PROJECT_DIR" || true
fi

# Exit code mirrors test outcome so CI fails on a red suite.
exit $RUN_EXIT
"""


def init_project(name: str, platform_name: str, target: dict, *,
                 parallel: int = 4, force: bool = False) -> Path:
    proj = project_dir(name)
    if proj.exists() and not force and (proj / ".qa-config.json").exists():
        print(f"Project already exists: {proj} (reusing; pass --force to reset config)")
    proj.mkdir(parents=True, exist_ok=True)

    for sub in PROJECT_DIRS:
        (proj / sub).mkdir(parents=True, exist_ok=True)

    cfg_path = proj / ".qa-config.json"
    if force or not cfg_path.exists():
        save_config(proj, {
            "project": name,
            "slug": slugify(name),
            "platform": platform_name,
            "target": target,
            "parallel": parallel,
            "env_file": ".env.qa",
            "created": now_iso(),
        })

    gi = proj / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)

    envf = proj / ".env.qa"
    if not envf.exists():
        envf.write_text(
            ENV_TEMPLATE
            .replace("__USER__", os.environ.get("USER", ""))
            .replace("__EMAIL__", os.environ.get("QA_USER_EMAIL", ""))
        )
        os.chmod(envf, 0o600)

    plan_path = proj / "test_cases" / "testplan.json"
    if not plan_path.exists():
        starter = dict(STARTER_PLANS.get(platform_name, STARTER_PLANS["web"]))
        plan = {
            "project": name,
            "platform": platform_name,
            "generated": now_iso(),
            "suites": starter["suites"],
        }
        plan_path.write_text(json.dumps(plan, indent=2) + "\n")

    runner = proj / "run_tests.sh"
    runner.write_text(RUN_TESTS_SH.format(
        project=name, platform=platform_name, agent_root=str(AGENT_ROOT),
    ))
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # automation_scripts/ holds the handoff copy referenced in the spec.
    link = proj / "automation_scripts" / "run_tests.sh"
    if not link.exists():
        try:
            link.symlink_to(runner)
        except OSError:
            link.write_text(runner.read_text())
            link.chmod(link.stat().st_mode | stat.S_IXUSR)

    return proj


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="qa init")
    ap.add_argument("--project", required=True)
    ap.add_argument("--platform", required=True, choices=("web", "extension", "android", "ios"))
    ap.add_argument("--target", default="{}", help="JSON target block")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    target = json.loads(args.target)
    proj = init_project(args.project, args.platform, target,
                        parallel=args.parallel, force=args.force)
    print(f"\nScaffolded: {proj}")
    for sub in PROJECT_DIRS:
        print(f"  {sub}/")
    print(f"  .qa-config.json   .env.qa   run_tests.sh\n")
    print(json.dumps({"project_dir": str(proj)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
