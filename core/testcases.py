"""Export testplan.json to the deliverable formats: Excel (.xlsx) and Markdown.

Columns follow the agent spec: Test Case ID, Description, Pre-conditions, Steps,
Expected Result, Test Type. Priority, Suite, and Tags are added because a real
suite is unusable without them.

When results.json exists, actual status / execution date / defect notes are
merged in, so the same command produces both the pre-execution test case doc and
the post-execution updated doc.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.common import (  # noqa: E402
    load_config, load_testplan, now_iso, slugify, summarize,
)

COLUMNS = [
    ("id", "Test Case ID", 16),
    ("suite", "Suite / Flow", 26),
    ("description", "Description", 52),
    ("type", "Test Type", 14),
    ("priority", "Priority", 9),
    ("preconditions", "Pre-conditions", 34),
    ("steps", "Steps", 62),
    ("expected", "Expected Result", 40),
    ("tags", "Tags", 16),
    ("status", "Actual Status", 13),
    ("actual", "Actual Result / Defect", 46),
    ("executed", "Executed On", 22),
]


def humanize_step(step: dict, index: int) -> str:
    """Render one DSL step as a numbered manual instruction, secrets masked."""
    action = step.get("action", "?")
    if step.get("note"):
        return f"{index}. {step['note']}"
    target = ""
    for key in ("selector", "role", "label", "placeholder", "testid", "text",
                "id", "accessibilityId", "xpath", "uiautomator", "resourceId", "desc"):
        if key in step:
            target = f"[{key}={step[key]}]"
            break
    raw = str(step.get("value", ""))
    secret = "${" in raw and any(s in raw.upper() for s in ("PASS", "TOKEN", "SECRET", "KEY"))
    val = "<secret>" if secret else raw

    verbs = {
        "goto": lambda: f"Navigate to {step.get('url', '/')}",
        "click": lambda: f"Click {target}",
        "dblclick": lambda: f"Double-click {target}",
        "tap": lambda: f"Tap {target}",
        "hover": lambda: f"Hover over {target}",
        "fill": lambda: f'Enter "{val}" in {target}',
        "type": lambda: f'Type "{val}" in {target}',
        "sendKeys": lambda: f'Enter "{val}" in {target}',
        "clear": lambda: f"Clear {target}",
        "select": lambda: f'Select "{val}" from {target}',
        "check": lambda: f"Check {target}",
        "uncheck": lambda: f"Uncheck {target}",
        "press": lambda: f"Press {step.get('key', 'Enter')} {target}".strip(),
        "upload": lambda: f"Upload {step.get('file') or step.get('files')} to {target}",
        "scroll": lambda: f"Scroll to {step.get('to', 'bottom')}",
        "scrollTo": lambda: f'Scroll until "{step.get("text")}" is visible',
        "swipe": lambda: f"Swipe {step.get('direction', 'up')} x{step.get('times', 1)}",
        "back": lambda: "Press back",
        "home": lambda: "Press home",
        "reload": lambda: "Reload the page",
        "wait": lambda: f"Wait {step.get('ms', 1000)}ms",
        "waitFor": lambda: f"Wait for {target} to be {step.get('state', 'visible')}",
        "screenshot": lambda: f"Capture screenshot ({step.get('name', 'shot')})",
        "setViewport": lambda: f"Resize viewport to {step.get('width')}x{step.get('height')}",
        "rotate": lambda: f"Rotate to {step.get('orientation', 'LANDSCAPE')}",
        "openPopup": lambda: "Open the extension popup",
        "openOptions": lambda: "Open the extension options page",
        "launchApp": lambda: "Launch the app",
        "terminateApp": lambda: "Terminate the app",
        "background": lambda: f"Background the app for {step.get('seconds', 3)}s",
        "api": lambda: f"{step.get('method', 'GET')} {step.get('url')} "
                       f"(expect {step.get('expectStatus', '2xx')})",
    }
    if action in verbs:
        return f"{index}. {verbs[action]()}"
    if action.startswith("expect"):
        what = action[6:]
        extra = step.get("text") or step.get("contains") or step.get("value") or ""
        return f"{index}. VERIFY {what} {target} {extra}".strip()
    return f"{index}. {action} {target}".strip()


def case_rows(plan: dict, results: dict | None):
    """Flatten the plan into row dicts, merging execution results when present."""
    res_by_id = {c.get("id"): c for c in (results or {}).get("cases", [])}
    exec_date = (results or {}).get("finished") or (results or {}).get("started") or ""
    rows = []
    for suite in plan.get("suites", []):
        for case in suite.get("cases", []):
            steps = case.get("steps", [])
            res = res_by_id.get(case.get("id"))
            actual = ""
            if res:
                if res.get("status") == "PASS":
                    actual = "Matches expected result."
                elif res.get("error"):
                    actual = res["error"]
                if res.get("screenshots"):
                    actual += f"\nEvidence: {len(res['screenshots'])} screenshot(s)"
            rows.append({
                "id": case.get("id", ""),
                "suite": f"{suite.get('id', '')} {suite.get('name', '')}".strip(),
                "description": case.get("description", ""),
                "type": case.get("type", "Functional"),
                "priority": case.get("priority", "P2"),
                "preconditions": case.get("preconditions", ""),
                "steps": "\n".join(humanize_step(s, i) for i, s in enumerate(steps, 1)),
                "expected": case.get("expected", ""),
                "tags": ", ".join(case.get("tags", [])),
                "status": (res or {}).get("status", "NOT RUN"),
                "actual": actual,
                "executed": exec_date if res else "",
            })
    return rows


def write_markdown(path: Path, plan: dict, rows: list, results: dict | None) -> Path:
    lines = [
        f"# Test Cases - {plan.get('project', 'project')}",
        "",
        f"- **Platform:** {plan.get('platform', '')}",
        f"- **Total cases:** {len(rows)}",
        f"- **Generated:** {plan.get('generated', now_iso())}",
    ]
    if results:
        s = summarize(results)
        lines += [
            f"- **Last execution:** {results.get('finished', '')}",
            f"- **Result:** {s['passed']}/{s['total']} passed ({s['pass_rate']}% of executed), "
            f"{s['failed']} failed, {s['blocked']} blocked",
        ]
    lines.append("")

    by_suite = {}
    for row in rows:
        by_suite.setdefault(row["suite"], []).append(row)

    for suite, srows in by_suite.items():
        lines += [f"## {suite or 'Ungrouped'}", ""]
        for row in srows:
            lines += [
                f"### {row['id']} - {row['description']}",
                "",
                f"| Field | Value |",
                f"|---|---|",
                f"| Test Type | {row['type']} |",
                f"| Priority | {row['priority']} |",
                f"| Tags | {row['tags'] or '-'} |",
                f"| Pre-conditions | {row['preconditions'] or '-'} |",
                f"| Expected Result | {row['expected'] or '-'} |",
                f"| Actual Status | **{row['status']}** |",
            ]
            if row["actual"]:
                lines.append(f"| Actual Result | {row['actual'].replace(chr(10), '<br>')} |")
            if row["executed"]:
                lines.append(f"| Executed On | {row['executed']} |")
            lines += ["", "**Steps**", ""]
            for step_line in (row["steps"].splitlines() or ["-"]):
                lines.append(f"{step_line}")
            lines.append("")
    path.write_text("\n".join(lines) + "\n")
    return path


def write_excel(path: Path, plan: dict, rows: list, results: dict | None) -> Path | None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("WARN: openpyxl missing - skipping .xlsx (pip3 install --user openpyxl)",
              file=sys.stderr)
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = "Test Cases"

    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    status_fills = {
        "PASS": PatternFill("solid", fgColor="C6EFCE"),
        "FAIL": PatternFill("solid", fgColor="FFC7CE"),
        "BLOCKED": PatternFill("solid", fgColor="FFEB9C"),
        "SKIP": PatternFill("solid", fgColor="E7E6E6"),
        "NOT RUN": PatternFill("solid", fgColor="F2F2F2"),
    }

    for col, (_, title, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 28

    for r, row in enumerate(rows, start=2):
        for c, (key, _, _) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=r, column=c, value=row.get(key, ""))
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if key == "status":
                cell.fill = status_fills.get(row.get("status", "NOT RUN"), status_fills["NOT RUN"])
                cell.font = Font(bold=True)
        # Roughly size the row to its tallest wrapped cell.
        step_lines = len(row["steps"].splitlines()) or 1
        ws.row_dimensions[r].height = min(15 * max(step_lines, 2), 220)

    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(rows) + 1}"

    # Summary sheet
    s2 = wb.create_sheet("Summary")
    s2.column_dimensions["A"].width = 28
    s2.column_dimensions["B"].width = 46
    meta = [
        ("Project", plan.get("project", "")),
        ("Platform", plan.get("platform", "")),
        ("Total test cases", len(rows)),
        ("Generated", plan.get("generated", now_iso())),
    ]
    if results:
        s = summarize(results)
        env = results.get("env", {})
        meta += [
            ("Executed on", results.get("finished", "")),
            ("Executed by", env.get("user", "")),
            ("Passed", s["passed"]),
            ("Failed", s["failed"]),
            ("Blocked", s["blocked"]),
            ("Pass rate (of executed)", f"{s['pass_rate']}%"),
            ("Execution coverage", f"{s['coverage']}%"),
            ("Environment", f"{env.get('os', '')} / {env.get('browser') or env.get('device', '')}"),
            ("Target", env.get("base_url") or env.get("app") or ""),
        ]
    for i, (k, v) in enumerate(meta, start=1):
        kc = s2.cell(row=i, column=1, value=k)
        kc.font = Font(bold=True)
        s2.cell(row=i, column=2, value=v)

    wb.save(path)
    return path


def main(argv):
    ap = argparse.ArgumentParser(prog="qa cases")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--format", default="both", choices=("md", "xlsx", "both"))
    args = ap.parse_args(argv)

    proj = Path(args.project_dir).resolve()
    cfg = load_config(proj)
    plan = load_testplan(proj)
    plan.setdefault("project", cfg.get("project"))
    plan.setdefault("platform", cfg.get("platform"))

    res_path = proj / "test_results" / "results.json"
    results = json.loads(res_path.read_text()) if res_path.exists() else None
    rows = case_rows(plan, results)

    out_dir = proj / "test_cases"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = slugify(cfg.get("project", "project"))
    written = []

    if args.format in ("md", "both"):
        written.append(write_markdown(out_dir / f"{base}-test-cases.md", plan, rows, results))
    if args.format in ("xlsx", "both"):
        xl = write_excel(out_dir / f"{base}-test-cases.xlsx", plan, rows, results)
        if xl:
            written.append(xl)

    for path in written:
        print(f"wrote {path}")
    print(f"{len(rows)} test case(s)" + (" (with execution results merged)" if results else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
