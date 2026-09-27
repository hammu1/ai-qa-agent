# AI QA Agent — multi-platform test automation framework

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-web%20%2B%20extension-2EAD33?style=flat-square">
  <img alt="Appium" src="https://img.shields.io/badge/Appium-Android%20%2B%20iOS-662D91?style=flat-square">
  <img alt="License" src="https://img.shields.io/badge/License-All%20Rights%20Reserved-lightgrey?style=flat-square">
</p>

One entrypoint that asks **what** you want to test, checks whether this machine can actually test it, scaffolds the project, executes a declarative test plan, and produces an Excel/Markdown test-case doc plus a PDF report with screenshots and reproduction steps.

Supported platforms: **web app**, **browser extension**, **Android**, **iOS** (iOS execution requires a macOS host — see [Platform support](#platform-support)).

---

## Quick start

```bash
git clone https://github.com/hammu1/ai-qa-agent.git
cd ai-qa-agent

./qa doctor              # what can this machine test right now?
./qa                     # interactive: pick a platform, answer a few questions
./qa full "My Project"   # run tests + export test cases + build the PDF report
```

That's the whole loop. Everything below is detail.

---

## The two ideas that make it work

**1. Test cases are data, not code.** `test_cases/testplan.json` holds suites → cases → declarative steps. That one file drives execution, the Excel/Markdown test-case document, *and* the PDF report. A manual tester who has never written Python can add coverage by editing JSON, and the documentation can never drift from what actually ran.

**2. Every runner writes an identical results contract.** That's why a single reporter and a single exporter serve all four platforms. Adding a platform means adding a runner that honours the contract — nothing downstream changes.

And one rule that keeps the numbers honest: **`BLOCKED` is a distinct status from `FAIL`, and it is excluded from the pass rate.** A test that never ran is never reported as passing. Execution coverage is printed alongside the pass rate, so a suite that mostly failed to start cannot look healthy.

---

## Commands

| Command | What it does |
|---|---|
| `./qa` / `./qa init` | Interactive setup: shows live capability per platform, asks what to test, scaffolds the project |
| `./qa doctor [platform]` | Capability report. Add `--json` for machine-readable output |
| `./qa run <project>` | Execute the test plan |
| `./qa cases <project>` | Export test cases to `.xlsx` + `.md` (merges results if a run exists) |
| `./qa report <project>` | Build the HTML + PDF report from the latest results |
| `./qa full <project>` | `run` + `cases` + `report` in one shot |
| `./qa list` | Every known project, its platform, case count, and last result |
| `./qa open <project>` | Print all artifact paths for a project |

Run flags (accepted by `run` / `full` / `run_tests.sh`):

| Flag | Effect |
|---|---|
| `--only TC-001,TC-007` | Run specific case IDs |
| `--tag smoke` | Run only cases carrying a tag |
| `--suite F-01` | Run one suite |
| `--headed` | Show the browser (web/extension) |
| `--parallel N` | Override worker count |
| `--no-report` | Skip report generation (`run_tests.sh` only) |

**Exit codes** — CI-friendly: `0` all passed · `1` failures · `2` blocked/environment problem · `3` no cases matched the filters.

---

## Project layout

Projects live in `~/qa-projects/<slug>/` (override with `QA_WORKSPACE`):

```
my-project/
├── .qa-config.json     platform + target (URL / APK / extension path) + parallelism
├── .env.qa             credentials, gitignored, chmod 600
├── run_tests.sh        portable standalone entrypoint — hand this to anyone
├── test_cases/
│   ├── testplan.json   the source of truth: suites → cases → steps
│   ├── *-test-cases.xlsx
│   └── *-test-cases.md
├── test_results/
│   ├── results.json    machine-readable results (+ timestamped archives)
│   ├── *-test-report-*.pdf
│   └── latest-report.pdf → newest PDF
├── screenshots/        one per capture + automatic capture at every failure
├── logs/               timestamped execution log per run (audit trail)
└── automation_scripts/ run_tests.sh (handoff copy)
```

### Handoff

`run_tests.sh` is self-contained and requires no GUI and no knowledge of this framework:

```bash
cd ~/qa-projects/my-project
./run_tests.sh                 # run everything + build the report
./run_tests.sh --tag smoke     # subset
./run_tests.sh --doctor        # verify this machine can run the suite
```

It sources `.env.qa`, dispatches to the right runner for the project's platform, and exits non-zero on a red suite — drop it straight into CI.

---

## Writing test plans

```json
{
  "project": "My Project",
  "platform": "web",
  "suites": [{
    "id": "F-01",
    "name": "Authentication",
    "cases": [{
      "id": "TC-WEB-002",
      "description": "Valid credentials land the user on the dashboard",
      "type": "Functional",
      "priority": "P1",
      "tags": ["smoke", "auth"],
      "preconditions": "A registered account exists",
      "expected": "User is redirected to /dashboard and sees the welcome banner",
      "steps": [
        {"action": "goto", "url": "/login"},
        {"action": "fill", "selector": "#email", "value": "${QA_USERNAME}"},
        {"action": "fill", "selector": "#pw", "value": "${QA_PASSWORD}"},
        {"action": "click", "role": "button", "name": "Sign in"},
        {"action": "expectUrl", "contains": "/dashboard"},
        {"action": "expectText", "selector": "#welcome", "text": "Welcome back"},
        {"action": "screenshot", "name": "dashboard"}
      ]
    }]
  }]
}
```

`${VAR}` resolves from `.env.qa` then the environment, so **credentials never live in the plan**. Values whose name suggests a secret (`PASS`, `TOKEN`, `SECRET`, `KEY`) are masked as `<secret>` in reports and repro steps.

Add `"note": "..."` to any step to override its description in the test-case doc and reproduction steps — useful for making an assertion's *intent* explicit.

### Targeting elements (web / extension)

Priority order: `role`+`name` → `label` → `placeholder` → `testid` → `text` → `selector` (CSS or XPath). Prefer roles and labels; they survive redesigns. `nth` and `has_text` refine a `selector`.

### Step actions

**Web / extension — interaction:** `goto` `click` `dblclick` `hover` `focus` `fill` `type` `clear` `press` `select` `check` `uncheck` `upload` `scroll` `wait` `waitFor` `waitForUrl` `waitForLoad` `reload` `back` `setViewport` `screenshot`

**Web / extension — assertions:** `expectVisible` `expectHidden` `expectText` `expectValue` `expectCount` (with `op`: `eq`/`gte`/`lte`/`gt`) `expectUrl` `expectTitle` `expectTitleNotEmpty` `expectAttribute` `expectEnabled` `expectDisabled` `expectNoConsoleErrors` `expectNoNetworkFailures` `expectLoadUnder`

**Web — API/integration:** `api` (`method`, `url`, `body`, `headers`, `expectStatus`, `expectBodyContains`) — shares the browser's cookies, so it tests authenticated endpoints as the logged-in user.

**Extension-only:** `expectExtensionLoaded` `openPopup` `openOptions` `openExtensionPage`

**Android / iOS — interaction:** `tap` `tapCoords` `longPress` `sendKeys` `clear` `hideKeyboard` `back` `home` `swipe` (`direction`, `times`) `scrollTo` `wait` `screenshot` `rotate` `launchApp` `terminateApp` `background` `openNotifications`

**Android / iOS — assertions:** `expectVisible` `expectHidden` `expectText` `expectEnabled` `expectAppRunning` `expectActivity` `expectNoCrash` `expectToast`

Mobile targeting keys: `id`/`resourceId`, `accessibilityId`/`desc`, `xpath`, `className`, `uiautomator` (Android), `predicate`/`classChain` (iOS), or `text` for a best-effort text/content-desc match.

---

## Platform support

| Platform | Driver | Notes |
|---|---|---|
| Web | Playwright (Python) | chromium / firefox / webkit / system chrome. Cases run in parallel, each in an isolated browser context |
| Browser extension | Playwright persistent context | MV3 needs a **headful** Chromium, so runs force `headless: false` and are serialized (one shared profile). Needs a display — use `xvfb-run` on headless CI |
| Android | Appium + UiAutomator2 | Needs a device or emulator online via `adb`. Multiple devices → cases shard across them in parallel. Starts/stops the Appium server automatically |
| iOS | Appium + XCUITest | **macOS + Xcode only.** On Linux, cases are recorded as `BLOCKED` with an explanation. To execute, point `target.server` at a macOS runner or a cloud device farm |

### Test statuses

- **PASS** / **FAIL** — the test actually ran; FAIL means a product defect.
- **BLOCKED** — the test *never ran* (no device, missing driver, unreachable target). Reported separately and **excluded from the pass rate**, because counting an unrun test as either pass or fail would be a lie.
- **SKIP** — deliberately not run.

---

## Reports

`./qa report <project>` produces a single portable PDF (screenshots embedded as base64, no sibling folders needed) containing:

1. Executive summary — pass/fail/blocked counts, pass rate, verdict
2. Environment & run details — project, user, host, OS, browser/device, target, timings
3. Full test case results table
4. Failure details: error, **numbered reproduction steps**, step-by-step execution log, failure screenshot, console errors, network failures
5. Blocked cases with remediation
6. Appendix: passed-case evidence

HTML is always written; if `weasyprint` is missing the PDF is reported as skipped with the install command — never silently dropped.

---

## Dependencies

Required: `python3`, `playwright` (+ browsers) for web/extension; `appium` + `Appium-Python-Client` + `adb` for Android. Reporting: `weasyprint` (PDF), `openpyxl` (Excel).

```bash
pip3 install --user playwright openpyxl weasyprint Appium-Python-Client
python3 -m playwright install chromium firefox
npm i -g appium && appium driver install uiautomator2   # Android only
```

On Debian/Ubuntu with an externally-managed Python (PEP 668), add `--break-system-packages` to `pip3 install --user`.

`./qa doctor` tells you exactly what is missing and the command to fix it.

---

## Architecture

```
qa                    bash router — interactive menu, capability gating, subcommands
qa_run.py             dispatcher: reads .qa-config.json → picks the runner
core/
  common.py           paths, config/env loading, ${VAR} interpolation,
                      the results contract, logging, summarize()
  doctor.py           per-platform capability detection + remediation hints
  scaffold.py         project folders, config, .env.qa, starter plan, run_tests.sh
  testcases.py        testplan.json → .xlsx + .md (merges results when present)
  report.py           results.json → HTML → PDF
runners/
  web_runner.py       Playwright engine for web + extension (ExtensionExecutor
                      subclasses WebExecutor)
  mobile_runner.py    Appium engine for Android + iOS
```

### Extending

- **New step action:** add a branch in the relevant executor's `run_step`, then a line in `core/testcases.py::humanize_step` so it reads correctly in the docs.
- **New platform:** add a runner writing the results contract, register it in `qa_run.py::RUNNERS`, and add a check to `core/doctor.py::CHECKS`.

---

## Related

- [`claude-qa-skills`](https://github.com/hammu1/claude-qa-skills) — AI agent workflows that drive this framework
- [`appium-framework`](https://github.com/hammu1/appium-framework) — Java + TestNG Android automation with a generic APK smoke crawler

---

## License

**All Rights Reserved** — see [LICENSE](LICENSE).

This code is published for portfolio and evaluation purposes. You're welcome to read it;
reuse in your own projects requires written permission.

Built by [Syed Hammad Ali](https://github.com/hammu1) · AI QA Engineer
