"""Platform dispatcher.

Reads .qa-config.json, picks the right runner, and forwards flags. Every runner
writes the same results.json, so callers (run_tests.sh, CI, the qa router) never
need to know which platform they are on.

Exit codes: 0 all passed | 1 failures | 2 blocked/environment | 3 nothing matched
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.common import load_config  # noqa: E402

RUNNERS = {
    "web": "runners.web_runner",
    "extension": "runners.web_runner",
    "android": "runners.mobile_runner",
    "ios": "runners.mobile_runner",
}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="qa_run", add_help=True)
    ap.add_argument("--project-dir", required=True)
    known, extra = ap.parse_known_args(argv)

    proj = Path(known.project_dir).resolve()
    cfg = load_config(proj)
    platform_name = cfg.get("platform")
    module = RUNNERS.get(platform_name)
    if not module:
        print(f"ERROR: unsupported platform '{platform_name}' in {proj}/.qa-config.json",
              file=sys.stderr)
        return 2

    import importlib
    runner = importlib.import_module(module)
    return runner.main(["--project-dir", str(proj)] + extra)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
