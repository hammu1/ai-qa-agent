"""Shared paths, config loading, and the results/testplan data contract.

Every runner (web, extension, android, ios) reads a project's .qa-config.json,
consumes test_cases/testplan.json, and writes test_results/results.json in the
exact same shape. That contract is what lets one reporter serve all platforms.
"""
import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.environ.get("QA_WORKSPACE", Path.home() / "qa-projects"))

PLATFORMS = ("web", "extension", "android", "ios")

# Folder layout created for every project, per the agent spec.
PROJECT_DIRS = (
    "test_cases",
    "test_results",
    "screenshots",
    "logs",
    "automation_scripts",
)

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_BLOCKED = "BLOCKED"
STATUS_SKIP = "SKIP"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(text: str) -> str:
    out = []
    for ch in str(text).strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " ._/:\\-":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "project"


def project_dir(name: str) -> Path:
    return WORKSPACE_ROOT / slugify(name)


def load_config(proj_dir: Path) -> dict:
    cfg_path = Path(proj_dir) / ".qa-config.json"
    if not cfg_path.exists():
        raise SystemExit(
            f"No .qa-config.json in {proj_dir}\n"
            f"Run:  qa init   (to scaffold this project first)"
        )
    with open(cfg_path) as fh:
        return json.load(fh)


def save_config(proj_dir: Path, cfg: dict) -> None:
    with open(Path(proj_dir) / ".qa-config.json", "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


def load_env_file(path) -> dict:
    """Parse a KEY=value .env file. Values are NOT exported globally, only
    returned, so credentials stay scoped to the run that needs them."""
    env = {}
    p = Path(path)
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def interpolate(value, env: dict):
    """Replace ${VAR} in step values from the env file, then the real environ.

    Keeps secrets out of testplan.json - plans reference ${QA_PASSWORD}, the
    value lives in .env.qa which is gitignored.
    """
    if not isinstance(value, str):
        return value
    out = value
    for key, val in env.items():
        out = out.replace("${%s}" % key, str(val))
    while "${" in out:
        start = out.index("${")
        end = out.find("}", start)
        if end == -1:
            break
        var = out[start + 2:end]
        out = out[:start] + os.environ.get(var, "") + out[end + 1:]
    return out


def load_testplan(proj_dir: Path) -> dict:
    path = Path(proj_dir) / "test_cases" / "testplan.json"
    if not path.exists():
        raise SystemExit(
            f"No testplan at {path}\n"
            f"Generate one with:  qa plan   (or ask the QA agent to author it)"
        )
    with open(path) as fh:
        plan = json.load(fh)
    if "suites" not in plan:
        raise SystemExit(f"{path} has no 'suites' key - not a valid testplan.")
    return plan


def iter_cases(plan: dict, only_ids=None, only_tags=None, only_suite=None):
    """Flatten suites -> (suite, case) honouring --only / --tag / --suite filters."""
    for suite in plan.get("suites", []):
        if only_suite and suite.get("id") != only_suite and suite.get("name") != only_suite:
            continue
        for case in suite.get("cases", []):
            if only_ids and case.get("id") not in only_ids:
                continue
            if only_tags and not (set(case.get("tags", [])) & set(only_tags)):
                continue
            yield suite, case


def _cmd(args) -> str:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=15
        ).stdout.strip().splitlines()[0]
    except Exception:
        return "n/a"


def environment_details(cfg: dict) -> dict:
    """Captured into every report so a failure is reproducible off this machine."""
    env = {
        "user": os.environ.get("QA_USER") or os.environ.get("USER") or "unknown",
        "user_email": os.environ.get("QA_USER_EMAIL", ""),
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "os_detail": platform.version(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "platform_under_test": cfg.get("platform"),
        "project": cfg.get("project"),
    }
    plat = cfg.get("platform")
    target = cfg.get("target", {})
    if plat in ("web", "extension"):
        env["base_url"] = target.get("base_url", "")
        env["browser"] = target.get("browser", "chromium")
        env["headless"] = target.get("headless", True)
        env["viewport"] = target.get("viewport", {})
        try:
            import playwright
            env["playwright"] = getattr(playwright, "__version__", "installed")
        except Exception:
            env["playwright"] = "missing"
        if plat == "extension":
            env["extension_path"] = target.get("extension_path", "")
    elif plat == "android":
        env["app"] = target.get("app") or target.get("appPackage", "")
        env["device"] = target.get("device", "auto")
        env["appium_server"] = target.get("server", "http://127.0.0.1:4723")
        env["adb"] = _cmd(["adb", "version"])
    elif plat == "ios":
        env["app"] = target.get("app", "")
        env["device"] = target.get("device", "")
    return env


def write_results(proj_dir: Path, results: dict) -> Path:
    out_dir = Path(proj_dir) / "test_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "results.json"
    with open(latest, "w") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    archived = out_dir / f"results-{stamp()}.json"
    with open(archived, "w") as fh:
        json.dump(results, fh, indent=2)
        fh.write("\n")
    return latest


def summarize(results: dict) -> dict:
    cases = results.get("cases", [])
    counts = {
        STATUS_PASS: 0,
        STATUS_FAIL: 0,
        STATUS_BLOCKED: 0,
        STATUS_SKIP: 0,
    }
    for case in cases:
        counts[case.get("status", STATUS_BLOCKED)] = counts.get(case.get("status", STATUS_BLOCKED), 0) + 1
    total = len(cases)
    executed = counts[STATUS_PASS] + counts[STATUS_FAIL]
    return {
        "total": total,
        "passed": counts[STATUS_PASS],
        "failed": counts[STATUS_FAIL],
        "blocked": counts[STATUS_BLOCKED],
        "skipped": counts[STATUS_SKIP],
        "executed": executed,
        "pass_rate": round(100.0 * counts[STATUS_PASS] / executed, 1) if executed else 0.0,
        "coverage": round(100.0 * executed / total, 1) if total else 0.0,
    }


class Log:
    """Tees runner output to stdout and <project>/logs/<platform>-<stamp>.log
    so every run leaves an audit trail."""

    def __init__(self, proj_dir: Path, platform_name: str):
        log_dir = Path(proj_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{platform_name}-{stamp()}.log"
        self._fh = open(self.path, "a")

    def __call__(self, msg: str) -> None:
        line = f"[{now_iso()}] {msg}"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)
