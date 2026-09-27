"""results.json -> professional HTML + PDF test report.

Screenshots are embedded as base64 so the PDF and HTML are single, portable
files a client can open with no sibling folders. If weasyprint is unavailable
the HTML is still produced and the PDF is reported as blocked - never silently
skipped.
"""
import argparse
import base64
import html
import json
import mimetypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.common import load_config, now_iso, slugify, stamp, summarize  # noqa: E402

CSS = """
@page { size: A4; margin: 14mm 12mm 16mm 12mm;
  @bottom-center { content: "Page " counter(page) " of " counter(pages);
                   font-size: 8pt; color: #667085; } }
* { box-sizing: border-box; }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 9.5pt; color: #1d2939;
       line-height: 1.45; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2mm; color: #0f2c52; }
h2 { font-size: 13pt; margin: 7mm 0 2.5mm; color: #0f2c52;
     border-bottom: 1.2pt solid #d0d5dd; padding-bottom: 1.5mm; }
h3 { font-size: 10.5pt; margin: 4mm 0 1.5mm; color: #182230; }
.sub { color: #475467; font-size: 9pt; margin-bottom: 5mm; }
table { width: 100%; border-collapse: collapse; margin: 2mm 0 4mm; }
th, td { border: 0.6pt solid #d0d5dd; padding: 1.6mm 2mm; text-align: left;
         vertical-align: top; font-size: 8.5pt; }
th { background: #f2f4f7; font-weight: bold; color: #101828; }
/* Keep a row's cells together - a row split across pages scrambles the columns. */
tr { page-break-inside: avoid; }
thead { display: table-header-group; }
tr.zebra td { background: #fafbfc; }
.meta td:first-child { width: 34%; font-weight: bold; background: #f9fafb; }
.cards { display: table; width: 100%; margin: 3mm 0 5mm; border-spacing: 2mm 0; }
.card { display: table-cell; width: 20%; text-align: center; padding: 3mm 1mm;
        border: 0.8pt solid #d0d5dd; border-radius: 2mm; background: #fcfcfd; }
.card .n { font-size: 17pt; font-weight: bold; display: block; }
.card .l { font-size: 7.5pt; color: #475467; text-transform: uppercase;
           letter-spacing: 0.4pt; }
.pass .n { color: #067647; } .fail .n { color: #b42318; }
.block .n { color: #b54708; } .skip .n { color: #475467; }
.rate .n { color: #0f2c52; }
.badge { display: inline-block; padding: 0.4mm 1.8mm; border-radius: 1mm;
         font-size: 7.5pt; font-weight: bold; }
.b-PASS { background: #d1fadf; color: #05603a; }
.b-FAIL { background: #fee4e2; color: #912018; }
.b-BLOCKED { background: #fef0c7; color: #93370d; }
.b-SKIP { background: #eaecf0; color: #344054; }
/* Failure cards are routinely taller than a page (log + screenshots), so they
   must be allowed to break - forcing avoid here produced blank pages. */
.failcard { border: 0.8pt solid #fda29b; border-left: 2.5pt solid #d92d20;
            border-radius: 1.5mm; padding: 3mm; margin: 0 0 5mm;
            background: #fffbfa; }
.blockcard { border: 0.8pt solid #fec84b; border-left: 2.5pt solid #dc6803;
             border-radius: 1.5mm; padding: 3mm; margin: 0 0 5mm; background: #fffcf5; }
.err { font-family: "DejaVu Sans Mono", monospace; font-size: 8pt; background: #fef3f2;
       border: 0.5pt solid #fda29b; padding: 2mm; border-radius: 1mm;
       white-space: pre-wrap; word-break: break-word; color: #912018; }
ol.repro { margin: 1.5mm 0 2mm 5mm; padding: 0; font-size: 8.5pt; }
ol.repro li { margin-bottom: 0.6mm; }
.shot { margin: 2mm 0; page-break-inside: avoid; }
.shot img { max-width: 100%; border: 0.6pt solid #d0d5dd; border-radius: 1mm; }
.shot .cap { font-size: 7.5pt; color: #475467; margin-top: 0.8mm; }
.small { font-size: 8pt; color: #475467; }
.logline { font-family: "DejaVu Sans Mono", monospace; font-size: 7.5pt;
           color: #344054; white-space: pre-wrap; word-break: break-all; }
.footer { margin-top: 8mm; border-top: 0.6pt solid #d0d5dd; padding-top: 2mm;
          font-size: 7.5pt; color: #667085; }
.pb { page-break-before: always; }
"""


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def embed_image(path: str, max_bytes: int = 3_500_000) -> str | None:
    """Inline a screenshot as a data URI so the report is one portable file."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    try:
        data = p.read_bytes()
        if len(data) > max_bytes:
            return None
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:
        return None


def shot_block(path: str, caption: str = "") -> str:
    uri = embed_image(path)
    name = Path(path).name
    if not uri:
        return (f'<div class="shot"><div class="cap">Screenshot (not embedded): '
                f'{esc(path)}</div></div>')
    return (f'<div class="shot"><img src="{uri}" alt="{esc(name)}"/>'
            f'<div class="cap">{esc(caption or name)}</div></div>')


def build_html(results: dict, cfg: dict) -> str:
    s = summarize(results)
    env = results.get("env", {})
    project = results.get("project") or cfg.get("project", "")
    plat = results.get("platform") or cfg.get("platform", "")
    cases = results.get("cases", [])
    failures = [c for c in cases if c.get("status") == "FAIL"]
    blocked = [c for c in cases if c.get("status") == "BLOCKED"]

    target = env.get("base_url") or env.get("app") or env.get("extension_path") or "n/a"
    dur = results.get("duration_ms", 0)
    duration = f"{dur / 1000:.1f}s" if dur else "n/a"

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<title>QA Test Report - {esc(project)}</title>",
        f"<style>{CSS}</style></head><body>",
        f"<h1>QA Test Execution Report</h1>",
        f"<div class='sub'><b>{esc(project)}</b> &nbsp;|&nbsp; platform: "
        f"<b>{esc(plat)}</b> &nbsp;|&nbsp; executed by <b>{esc(env.get('user', ''))}</b>"
        f"{(' &lt;' + esc(env.get('user_email')) + '&gt;') if env.get('user_email') else ''}"
        f" &nbsp;|&nbsp; {esc(results.get('finished') or results.get('started') or now_iso())}</div>",
    ]

    # ---- executive summary -------------------------------------------------
    parts.append("<h2>1. Executive summary</h2>")
    parts.append(
        "<div class='cards'>"
        f"<div class='card pass'><span class='n'>{s['passed']}</span><span class='l'>Passed</span></div>"
        f"<div class='card fail'><span class='n'>{s['failed']}</span><span class='l'>Failed</span></div>"
        f"<div class='card block'><span class='n'>{s['blocked']}</span><span class='l'>Blocked</span></div>"
        f"<div class='card skip'><span class='n'>{s['skipped']}</span><span class='l'>Skipped</span></div>"
        f"<div class='card rate'><span class='n'>{s['pass_rate']}%</span><span class='l'>Pass rate</span></div>"
        "</div>"
    )
    verdict = ("All executed test cases passed." if s["failed"] == 0 and s["blocked"] == 0
               else f"{s['failed']} test case(s) failed"
                    + (f" and {s['blocked']} were blocked" if s["blocked"] else "")
                    + " - see sections 4 and 5.")
    parts.append(
        f"<p><b>Verdict:</b> {esc(verdict)}<br>"
        f"<span class='small'>{s['total']} case(s) in scope, {s['executed']} executed "
        f"({s['coverage']}% execution coverage). Pass rate is computed over executed "
        f"cases only; blocked cases are excluded because they never ran.</span></p>"
    )

    # ---- environment -------------------------------------------------------
    parts.append("<h2>2. Environment &amp; run details</h2><table class='meta'>")
    rows = [
        ("Project", project),
        ("Platform under test", plat),
        ("Target", target),
        ("Executed by", f"{env.get('user', '')} {env.get('user_email', '')}".strip()),
        ("Host machine", f"{env.get('hostname', '')} ({env.get('arch', '')})"),
        ("Operating system", env.get("os", "")),
        ("Started", results.get("started", "")),
        ("Finished", results.get("finished", "")),
        ("Total duration", duration),
    ]
    if plat in ("web", "extension"):
        vp = env.get("viewport") or {}
        rows += [
            ("Browser", f"{env.get('browser', '')} (Playwright {env.get('playwright', '')})"),
            ("Headless", str(env.get("headless", ""))),
            ("Viewport", f"{vp.get('width', '')}x{vp.get('height', '')}" if vp else ""),
        ]
        if plat == "extension":
            rows.append(("Extension path", env.get("extension_path", "")))
    else:
        rows += [
            ("Device", env.get("device", "")),
            ("App", env.get("app", "")),
            ("Appium server", env.get("appium_server", "")),
            ("adb", env.get("adb", "")),
        ]
    filters = results.get("filters") or {}
    active = {k: v for k, v in filters.items() if v}
    if active:
        rows.append(("Filters applied", json.dumps(active)))
    for key, val in rows:
        if val not in ("", None):
            parts.append(f"<tr><td>{esc(key)}</td><td>{esc(val)}</td></tr>")
    parts.append("</table>")

    # ---- results table -----------------------------------------------------
    parts.append("<h2>3. Test case results</h2><table>")
    parts.append("<tr><th style='width:12%'>ID</th><th style='width:20%'>Suite</th>"
                 "<th>Description</th><th style='width:11%'>Type</th>"
                 "<th style='width:7%'>Pri</th><th style='width:10%'>Status</th>"
                 "<th style='width:9%'>Duration</th></tr>")
    for i, c in enumerate(cases):
        st = c.get("status", "BLOCKED")
        zebra = " class='zebra'" if i % 2 else ""
        parts.append(
            f"<tr{zebra}><td>{esc(c.get('id'))}</td><td>{esc(c.get('suite'))}</td>"
            f"<td>{esc(c.get('description'))}</td><td>{esc(c.get('type'))}</td>"
            f"<td>{esc(c.get('priority'))}</td>"
            f"<td><span class='badge b-{esc(st)}'>{esc(st)}</span></td>"
            f"<td>{c.get('duration_ms', 0) / 1000:.1f}s</td></tr>"
        )
    parts.append("</table>")

    # ---- failures ----------------------------------------------------------
    parts.append("<h2>4. Failure details &amp; reproduction</h2>")
    if not failures:
        parts.append("<p>No failures. Every executed test case passed.</p>")
    for c in failures:
        parts.append("<div class='failcard'>")
        parts.append(f"<h3>{esc(c.get('id'))} &mdash; {esc(c.get('description'))} "
                     f"<span class='badge b-FAIL'>FAIL</span></h3>")
        parts.append("<table class='meta'>")
        for key, val in (("Suite", c.get("suite")), ("Test type", c.get("type")),
                         ("Priority", c.get("priority")),
                         ("Pre-conditions", c.get("preconditions")),
                         ("Expected result", c.get("expected")),
                         ("Failed at step", c.get("failed_step")),
                         ("Final URL", c.get("final_url")),
                         ("Device", c.get("device"))):
            if val not in ("", None):
                parts.append(f"<tr><td>{esc(key)}</td><td>{esc(val)}</td></tr>")
        parts.append("</table>")
        parts.append(f"<p><b>Error</b></p><div class='err'>{esc(c.get('error'))}</div>")

        if c.get("repro"):
            parts.append("<p><b>Steps to reproduce</b></p><ol class='repro'>")
            for line in c["repro"]:
                # repro lines are pre-numbered; strip the prefix for <ol>
                text = line.split(". ", 1)[1] if ". " in line[:5] else line
                parts.append(f"<li>{esc(text)}</li>")
            parts.append("</ol>")

        steps = c.get("steps", [])
        if steps:
            parts.append("<p><b>Step-by-step execution log</b></p><table>")
            parts.append("<tr><th style='width:6%'>#</th><th style='width:14%'>Action</th>"
                         "<th>Step</th><th style='width:10%'>Status</th><th>Detail</th></tr>")
            for st_row in steps:
                parts.append(
                    f"<tr><td>{esc(st_row.get('index'))}</td>"
                    f"<td>{esc(st_row.get('action'))}</td>"
                    f"<td>{esc(st_row.get('description'))}</td>"
                    f"<td><span class='badge b-{esc(st_row.get('status'))}'>"
                    f"{esc(st_row.get('status'))}</span></td>"
                    f"<td class='small'>{esc(st_row.get('detail'))}</td></tr>"
                )
            parts.append("</table>")

        fail_shots = [st_row.get("screenshot") for st_row in steps
                      if st_row.get("status") == "FAIL" and st_row.get("screenshot")]
        others = [p for p in c.get("screenshots", []) if p not in fail_shots]
        if fail_shots:
            parts.append("<p><b>Evidence at point of failure</b></p>")
            for path in fail_shots:
                parts.append(shot_block(path, f"{c.get('id')} - failure state"))
        if others:
            parts.append("<p><b>Preceding screenshots</b></p>")
            for path in others[:4]:
                parts.append(shot_block(path))

        if c.get("console_errors"):
            parts.append("<p><b>Console / device errors</b></p>")
            for line in c["console_errors"][:10]:
                parts.append(f"<div class='logline'>{esc(line)}</div>")
        if c.get("network_failures"):
            parts.append("<p><b>Network failures</b></p>")
            for line in c["network_failures"][:10]:
                parts.append(f"<div class='logline'>{esc(line)}</div>")
        parts.append("</div>")

    # ---- blocked -----------------------------------------------------------
    if blocked:
        parts.append("<h2>5. Blocked test cases</h2>")
        parts.append("<p class='small'>These cases never executed - an environment or harness "
                     "problem prevented the run. They are <b>not</b> product defects and are "
                     "excluded from the pass rate.</p>")
        for c in blocked:
            parts.append("<div class='blockcard'>")
            parts.append(f"<h3>{esc(c.get('id'))} &mdash; {esc(c.get('description'))} "
                         f"<span class='badge b-BLOCKED'>BLOCKED</span></h3>")
            parts.append(f"<div class='err'>{esc(c.get('error'))}</div>")
            parts.append("</div>")

    # ---- passed evidence appendix -----------------------------------------
    passed_with_shots = [c for c in cases if c.get("status") == "PASS" and c.get("screenshots")]
    if passed_with_shots:
        parts.append("<h2 class='pb'>6. Appendix - passed case evidence</h2>")
        for c in passed_with_shots:
            parts.append(f"<h3>{esc(c.get('id'))} &mdash; {esc(c.get('description'))} "
                         f"<span class='badge b-PASS'>PASS</span></h3>")
            for path in c["screenshots"][:3]:
                parts.append(shot_block(path))

    parts.append(
        f"<div class='footer'>Generated by QA Automation Agent on {esc(now_iso())} &middot; "
        f"project <b>{esc(project)}</b> &middot; results source: test_results/results.json "
        f"&middot; reproducible via <code>./run_tests.sh</code></div>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def to_pdf(html_text: str, pdf_path: Path, base_url: str) -> tuple[bool, str]:
    try:
        from weasyprint import HTML
    except ImportError:
        return False, ("weasyprint not installed - PDF skipped. "
                       "Install with: pip3 install --user weasyprint")
    try:
        HTML(string=html_text, base_url=base_url).write_pdf(str(pdf_path))
        return True, str(pdf_path)
    except Exception as exc:
        return False, f"PDF generation failed: {exc}"


def main(argv):
    ap = argparse.ArgumentParser(prog="qa report")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--results", default="", help="specific results json (default: latest)")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args(argv)

    proj = Path(args.project_dir).resolve()
    cfg = load_config(proj)
    res_path = Path(args.results) if args.results else proj / "test_results" / "results.json"
    if not res_path.exists():
        print(f"ERROR: no results at {res_path}. Run the tests first.", file=sys.stderr)
        return 2
    results = json.loads(res_path.read_text())

    out_dir = proj / "test_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{slugify(cfg.get('project', 'project'))}-test-report-{stamp()}"

    html_text = build_html(results, cfg)
    html_path = out_dir / f"{base}.html"
    html_path.write_text(html_text)
    print(f"wrote {html_path}")

    if not args.no_pdf:
        pdf_path = out_dir / f"{base}.pdf"
        ok, msg = to_pdf(html_text, pdf_path, base_url=str(proj))
        if ok:
            size = pdf_path.stat().st_size
            print(f"wrote {pdf_path} ({size / 1024:.0f} KB)")
            latest = out_dir / "latest-report.pdf"
            try:
                if latest.exists() or latest.is_symlink():
                    latest.unlink()
                latest.symlink_to(pdf_path.name)
            except OSError:
                pass
        else:
            print(f"WARN: {msg}", file=sys.stderr)

    s = summarize(results)
    print(f"summary: {s['passed']} passed / {s['failed']} failed / {s['blocked']} blocked "
          f"({s['pass_rate']}% pass rate of executed)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
