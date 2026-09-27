"""Web + browser-extension runner (Playwright, Python sync API).

Executes the declarative step DSL from testplan.json. One browser context per
test case = full isolation; cases run concurrently in a thread pool bounded by
config "parallel". Every failure captures a screenshot, the console log, failed
network requests, and generated repro steps.

Extension mode differs in exactly two ways: it launches a persistent context
with --load-extension (MV3 needs headful Chromium), and it exposes
openPopup / openOptions / expectExtensionLoaded actions.
"""
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.common import (  # noqa: E402
    STATUS_BLOCKED, STATUS_FAIL, STATUS_PASS, Log, environment_details,
    interpolate, iter_cases, load_config, load_env_file, load_testplan,
    now_iso, slugify, write_results,
)

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip3 install --user playwright "
          "&& python3 -m playwright install chromium", file=sys.stderr)
    raise SystemExit(2)


class StepFailure(Exception):
    """Raised when an assertion or action fails - carries the human-readable reason."""


def resolve_locator(page, step):
    """Turn a step's targeting keys into a Playwright locator.

    Supports (in priority order): role+name, label, placeholder, testid, text,
    then raw selector (css / xpath / playwright engine syntax).
    """
    if "role" in step:
        return page.get_by_role(step["role"], name=step.get("name"),
                                exact=step.get("exact", False))
    if "label" in step:
        return page.get_by_label(step["label"], exact=step.get("exact", False))
    if "placeholder" in step:
        return page.get_by_placeholder(step["placeholder"])
    if "testid" in step:
        return page.get_by_test_id(step["testid"])
    if "text" in step and "selector" not in step:
        return page.get_by_text(step["text"], exact=step.get("exact", False))
    sel = step.get("selector")
    if not sel:
        raise StepFailure(f"step '{step.get('action')}' has no target "
                          f"(need selector/role/label/placeholder/testid/text)")
    loc = page.locator(sel)
    if "has_text" in step:
        loc = page.locator(sel, has_text=step["has_text"])
    if step.get("nth") is not None:
        loc = loc.nth(int(step["nth"]))
    return loc


def describe_target(step) -> str:
    for key in ("selector", "role", "label", "placeholder", "testid", "text"):
        if key in step:
            val = step[key]
            if key == "role" and "name" in step:
                return f'{key}={val} name="{step["name"]}"'
            return f"{key}={val}"
    return "<no target>"


def describe_step(step, env) -> str:
    """Human-readable step line - reused verbatim as a repro instruction."""
    action = step.get("action", "?")
    if step.get("note"):
        return f"{action}: {step['note']}"
    if action == "goto":
        return f"Navigate to {step.get('url', '/')}"
    if action in ("click", "dblclick", "hover", "check", "uncheck", "focus"):
        return f"{action.capitalize()} {describe_target(step)}"
    if action in ("fill", "type"):
        raw = str(step.get("value", ""))
        # Never leak a credential into a report or repro step.
        shown = "<secret>" if "${" in raw and any(
            s in raw.upper() for s in ("PASS", "TOKEN", "SECRET", "KEY")) else raw
        return f"Enter \"{shown}\" into {describe_target(step)}"
    if action == "press":
        return f"Press {step.get('key')}" + (f" on {describe_target(step)}" if step.get("selector") else "")
    if action == "select":
        return f"Select \"{step.get('value')}\" in {describe_target(step)}"
    if action == "screenshot":
        return f"Capture screenshot '{step.get('name', 'shot')}'"
    if action == "wait":
        return f"Wait {step.get('ms', 1000)}ms"
    if action.startswith("expect"):
        extra = step.get("text") or step.get("contains") or step.get("value") or ""
        tgt = describe_target(step) if any(
            k in step for k in ("selector", "role", "label", "placeholder", "testid", "text")) else ""
        return f"Assert {action[6:]} {tgt} {extra}".strip()
    return f"{action} {json.dumps({k: v for k, v in step.items() if k != 'action'})}"


class WebExecutor:
    """Runs one test case in its own page. Isolated from all other cases."""

    def __init__(self, page, cfg, env, shot_dir, case_id, log):
        self.page = page
        self.cfg = cfg
        self.env = env
        self.shot_dir = shot_dir
        self.case_id = case_id
        self.log = log
        self.base_url = (cfg.get("target", {}).get("base_url") or "").rstrip("/")
        self.console_errors = []
        self.network_failures = []
        self.screenshots = []
        self._shot_seq = 0
        page.on("console", self._on_console)
        page.on("pageerror", lambda exc: self.console_errors.append(f"pageerror: {exc}"))
        page.on("response", self._on_response)
        page.on("requestfailed", lambda req: self.network_failures.append(
            f"{req.method} {req.url} failed: {req.failure}"))

    def _on_console(self, msg):
        if msg.type in ("error",):
            self.console_errors.append(f"console.{msg.type}: {msg.text}")

    def _on_response(self, resp):
        try:
            if resp.status >= 400:
                self.network_failures.append(f"HTTP {resp.status} {resp.request.method} {resp.url}")
        except Exception:
            pass

    def url_for(self, raw) -> str:
        raw = interpolate(raw or "/", self.env)
        if raw.startswith(("http://", "https://", "file://", "chrome-extension://", "about:")):
            return raw
        if not self.base_url:
            raise StepFailure(f"relative url '{raw}' but no target.base_url configured")
        return f"{self.base_url}/{raw.lstrip('/')}"

    def shot(self, name) -> str:
        self._shot_seq += 1
        fname = f"{slugify(self.case_id)}-{self._shot_seq:02d}-{slugify(name)}.png"
        path = self.shot_dir / fname
        try:
            self.page.screenshot(path=str(path), full_page=False)
            self.screenshots.append(str(path))
            return str(path)
        except Exception as exc:
            self.log(f"  ! screenshot failed: {exc}")
            return ""

    def run_step(self, step):
        action = step.get("action")
        timeout = int(step.get("timeout", self.cfg.get("target", {}).get("timeout", 15000)))
        page = self.page

        if action == "goto":
            resp = page.goto(self.url_for(step.get("url")),
                             wait_until=step.get("wait_until", "domcontentloaded"),
                             timeout=timeout)
            if resp is not None and resp.status >= 400 and not step.get("allow_error_status"):
                raise StepFailure(f"navigation returned HTTP {resp.status} for {resp.url}")
            return f"loaded {page.url}"

        if action == "click":
            resolve_locator(page, step).click(timeout=timeout, force=step.get("force", False))
            return f"clicked {describe_target(step)}"
        if action == "dblclick":
            resolve_locator(page, step).dblclick(timeout=timeout)
            return "double-clicked"
        if action == "hover":
            resolve_locator(page, step).hover(timeout=timeout)
            return "hovered"
        if action == "focus":
            resolve_locator(page, step).focus(timeout=timeout)
            return "focused"
        if action == "fill":
            resolve_locator(page, step).fill(interpolate(str(step.get("value", "")), self.env),
                                             timeout=timeout)
            return "filled"
        if action == "type":
            resolve_locator(page, step).press_sequentially(
                interpolate(str(step.get("value", "")), self.env),
                delay=int(step.get("delay", 30)), timeout=timeout)
            return "typed"
        if action == "clear":
            resolve_locator(page, step).fill("", timeout=timeout)
            return "cleared"
        if action == "press":
            key = step.get("key", "Enter")
            if any(k in step for k in ("selector", "role", "label", "placeholder", "testid")):
                resolve_locator(page, step).press(key, timeout=timeout)
            else:
                page.keyboard.press(key)
            return f"pressed {key}"
        if action == "select":
            val = interpolate(str(step.get("value", "")), self.env)
            resolve_locator(page, step).select_option(val, timeout=timeout)
            return f"selected {val}"
        if action == "check":
            resolve_locator(page, step).check(timeout=timeout)
            return "checked"
        if action == "uncheck":
            resolve_locator(page, step).uncheck(timeout=timeout)
            return "unchecked"
        if action == "upload":
            files = step.get("files") or [step.get("file")]
            resolve_locator(page, step).set_input_files([interpolate(f, self.env) for f in files if f],
                                                        timeout=timeout)
            return "uploaded"
        if action == "scroll":
            to = step.get("to", "bottom")
            if to == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif to == "top":
                page.evaluate("window.scrollTo(0, 0)")
            else:
                resolve_locator(page, {"selector": to}).scroll_into_view_if_needed(timeout=timeout)
            return f"scrolled {to}"
        if action == "wait":
            page.wait_for_timeout(int(step.get("ms", 1000)))
            return "waited"
        if action == "waitFor":
            resolve_locator(page, step).wait_for(state=step.get("state", "visible"), timeout=timeout)
            return "element appeared"
        if action == "waitForUrl":
            page.wait_for_url(re.compile(step["regex"]) if step.get("regex")
                              else self.url_for(step.get("url")), timeout=timeout)
            return f"url is {page.url}"
        if action == "waitForLoad":
            page.wait_for_load_state(step.get("state", "networkidle"), timeout=timeout)
            return "load state reached"
        if action == "reload":
            page.reload(timeout=timeout)
            return "reloaded"
        if action == "back":
            page.go_back(timeout=timeout)
            return "went back"
        if action == "setViewport":
            page.set_viewport_size({"width": int(step["width"]), "height": int(step["height"])})
            return f"viewport {step['width']}x{step['height']}"
        if action == "screenshot":
            return f"screenshot -> {self.shot(step.get('name', 'shot'))}"

        # ---- assertions -----------------------------------------------------
        if action == "expectVisible":
            loc = resolve_locator(page, step)
            loc.first.wait_for(state="visible", timeout=timeout)
            return f"{describe_target(step)} is visible"
        if action == "expectHidden":
            resolve_locator(page, step).first.wait_for(state="hidden", timeout=timeout)
            return f"{describe_target(step)} is hidden"
        if action == "expectText":
            want = interpolate(str(step.get("text", "")), self.env)
            loc = resolve_locator(page, step).first
            loc.wait_for(state="visible", timeout=timeout)
            got = (loc.inner_text() or "").strip()
            if want.lower() not in got.lower():
                raise StepFailure(f"expected text containing '{want}' in {describe_target(step)}, got '{got[:200]}'")
            return f"text contains '{want}'"
        if action == "expectValue":
            want = interpolate(str(step.get("value", "")), self.env)
            got = resolve_locator(page, step).first.input_value(timeout=timeout)
            if got != want:
                raise StepFailure(f"expected value '{want}', got '{got}'")
            return "value matches"
        if action == "expectCount":
            want = int(step.get("count", 0))
            got = resolve_locator(page, step).count()
            op = step.get("op", "eq")
            ok = {"eq": got == want, "gte": got >= want, "lte": got <= want, "gt": got > want}.get(op, got == want)
            if not ok:
                raise StepFailure(f"expected count {op} {want} for {describe_target(step)}, got {got}")
            return f"count {got}"
        if action == "expectUrl":
            cur = page.url
            if step.get("regex"):
                if not re.search(step["regex"], cur):
                    raise StepFailure(f"url '{cur}' does not match /{step['regex']}/")
            elif step.get("contains"):
                want = interpolate(step["contains"], self.env)
                if want not in cur:
                    raise StepFailure(f"url '{cur}' does not contain '{want}'")
            else:
                want = self.url_for(step.get("url", "/"))
                if cur.rstrip("/") != want.rstrip("/"):
                    raise StepFailure(f"url is '{cur}', expected '{want}'")
            return f"url ok ({cur})"
        if action == "expectTitle":
            title = page.title()
            want = interpolate(step.get("contains", step.get("text", "")), self.env)
            if want.lower() not in title.lower():
                raise StepFailure(f"title '{title}' does not contain '{want}'")
            return f"title '{title}'"
        if action == "expectTitleNotEmpty":
            title = (page.title() or "").strip()
            if not title:
                raise StepFailure("document title is empty")
            return f"title '{title}'"
        if action == "expectNoConsoleErrors":
            ignore = [re.compile(p) for p in step.get("ignore", [])]
            real = [e for e in self.console_errors
                    if not any(p.search(e) for p in ignore)]
            if real:
                raise StepFailure(f"{len(real)} console error(s): " + " | ".join(real[:5]))
            return "no console errors"
        if action == "expectNoNetworkFailures":
            ignore = [re.compile(p) for p in step.get("ignore", [])]
            real = [f for f in self.network_failures if not any(p.search(f) for p in ignore)]
            if real:
                raise StepFailure(f"{len(real)} network failure(s): " + " | ".join(real[:5]))
            return "no network failures"
        if action == "expectAttribute":
            got = resolve_locator(page, step).first.get_attribute(step["attribute"])
            want = interpolate(str(step.get("value", "")), self.env)
            if got != want:
                raise StepFailure(f"attribute {step['attribute']}='{got}', expected '{want}'")
            return "attribute matches"
        if action == "expectEnabled":
            if not resolve_locator(page, step).first.is_enabled(timeout=timeout):
                raise StepFailure(f"{describe_target(step)} is disabled")
            return "enabled"
        if action == "expectDisabled":
            if resolve_locator(page, step).first.is_enabled(timeout=timeout):
                raise StepFailure(f"{describe_target(step)} is enabled, expected disabled")
            return "disabled"

        # ---- API / perf -----------------------------------------------------
        if action == "api":
            method = step.get("method", "GET").upper()
            url = self.url_for(step.get("url"))
            body = step.get("body")
            req = page.request
            kwargs = {"timeout": timeout}
            if body is not None:
                kwargs["data"] = json.loads(interpolate(json.dumps(body), self.env)) \
                    if isinstance(body, (dict, list)) else interpolate(str(body), self.env)
            if step.get("headers"):
                kwargs["headers"] = {k: interpolate(str(v), self.env)
                                     for k, v in step["headers"].items()}
            resp = getattr(req, method.lower())(url, **kwargs)
            want = step.get("expectStatus")
            if want and resp.status != int(want):
                raise StepFailure(f"{method} {url} -> HTTP {resp.status}, expected {want}")
            if not want and resp.status >= 400:
                raise StepFailure(f"{method} {url} -> HTTP {resp.status}")
            if step.get("expectBodyContains"):
                text = resp.text()
                want_body = interpolate(step["expectBodyContains"], self.env)
                if want_body not in text:
                    raise StepFailure(f"response body missing '{want_body}'")
            return f"{method} {url} -> {resp.status}"
        if action == "expectLoadUnder":
            budget = int(step.get("ms", 3000))
            timing = page.evaluate(
                "() => { const n = performance.getEntriesByType('navigation')[0];"
                " return n ? Math.round(n.duration) : null; }")
            if timing is None:
                raise StepFailure("no navigation timing available")
            if timing > budget:
                raise StepFailure(f"page load {timing}ms exceeds budget {budget}ms")
            return f"load {timing}ms (budget {budget}ms)"

        # ---- extension-only -------------------------------------------------
        if action in ("openPopup", "openOptions", "expectExtensionLoaded", "openExtensionPage"):
            return self.run_extension_step(step, timeout)

        raise StepFailure(f"unknown action '{action}'")

    def run_extension_step(self, step, timeout):
        raise StepFailure(f"action '{step.get('action')}' is only valid for platform 'extension'")


class ExtensionExecutor(WebExecutor):
    """Adds MV3 extension awareness on top of the web engine."""

    def __init__(self, *args, extension_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.extension_id = extension_id

    def run_extension_step(self, step, timeout):
        action = step.get("action")
        if action == "expectExtensionLoaded":
            if not self.extension_id:
                raise StepFailure("extension id could not be resolved - the service worker "
                                  "never registered (check manifest.json and background script)")
            return f"extension loaded (id={self.extension_id})"
        if not self.extension_id:
            raise StepFailure("no extension id - cannot open extension pages")
        page_name = {"openPopup": step.get("page", "popup.html"),
                     "openOptions": step.get("page", "options.html")}.get(
                        action, step.get("page", ""))
        url = f"chrome-extension://{self.extension_id}/{page_name.lstrip('/')}"
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if resp is not None and resp.status >= 400:
            raise StepFailure(f"{page_name} returned HTTP {resp.status}")
        body = (self.page.inner_text("body") or "").strip()
        if action in ("openPopup", "openOptions") and not body and not step.get("allow_empty"):
            raise StepFailure(f"{page_name} rendered no visible content")
        return f"opened {url}"


def build_repro(case, steps_done, failed_step, cfg, env_details) -> list:
    """Numbered, copy-pasteable reproduction instructions for a failure."""
    lines = []
    target = cfg.get("target", {})
    if cfg.get("platform") == "extension":
        lines.append(f"1. Load the unpacked extension from {target.get('extension_path', '<path>')} "
                     f"in Chrome via chrome://extensions (Developer mode ON)")
    else:
        lines.append(f"1. Open {target.get('browser', 'chromium')} at {target.get('base_url', '<base_url>')} "
                     f"(viewport {env_details.get('viewport', {}).get('width', 1440)}x"
                     f"{env_details.get('viewport', {}).get('height', 900)})")
    if case.get("preconditions"):
        lines.append(f"2. Precondition: {case['preconditions']}")
    n = len(lines) + 1
    for desc in steps_done:
        lines.append(f"{n}. {desc}")
        n += 1
    if failed_step:
        lines.append(f"{n}. {failed_step}   <-- FAILS HERE")
    return lines


def make_context(browser_or_pw, cfg, plat, log):
    target = cfg.get("target", {})
    viewport = target.get("viewport") or {"width": 1440, "height": 900}
    kwargs = {
        "viewport": viewport,
        "ignore_https_errors": bool(target.get("ignore_https_errors", True)),
        "locale": target.get("locale", "en-US"),
    }
    if target.get("user_agent"):
        kwargs["user_agent"] = target["user_agent"]
    state = target.get("storage_state")
    if state and Path(state).exists():
        kwargs["storage_state"] = state
    return browser_or_pw.new_context(**kwargs)


def run_case(pw, cfg, plan_env, shot_dir, suite, case, log, lock, ext_ctx=None, ext_id=None):
    """Execute a single case. Returns the result dict for results.json."""
    case_id = case.get("id", "TC-?")
    started = time.time()
    result = {
        "id": case_id,
        "suite": suite.get("name", suite.get("id", "")),
        "suite_id": suite.get("id", ""),
        "description": case.get("description", ""),
        "type": case.get("type", "Functional"),
        "priority": case.get("priority", "P2"),
        "tags": case.get("tags", []),
        "preconditions": case.get("preconditions", ""),
        "expected": case.get("expected", ""),
        "status": STATUS_BLOCKED,
        "steps": [],
        "screenshots": [],
        "console_errors": [],
        "network_failures": [],
        "error": "",
        "repro": [],
        "started": now_iso(),
    }
    plat = cfg.get("platform")
    context = None
    own_context = False
    try:
        if plat == "extension":
            context = ext_ctx  # persistent context is shared; extensions can't be per-case
        else:
            browser = pw._qa_browser
            context = make_context(browser, cfg, plat, log)
            own_context = True

        page = context.new_page()
        ExecClass = ExtensionExecutor if plat == "extension" else WebExecutor
        ex = (ExecClass(page, cfg, plan_env, shot_dir, case_id, log, extension_id=ext_id)
              if plat == "extension" else ExecClass(page, cfg, plan_env, shot_dir, case_id, log))

        descs_done = []
        for idx, step in enumerate(case.get("steps", []), start=1):
            desc = describe_step(step, plan_env)
            try:
                detail = ex.run_step(step)
                result["steps"].append({"index": idx, "action": step.get("action"),
                                        "description": desc, "status": STATUS_PASS,
                                        "detail": str(detail)[:500]})
                descs_done.append(desc)
            except (StepFailure, PWTimeout, PWError, AssertionError) as exc:
                reason = str(exc).split("\n")[0][:600]
                if isinstance(exc, PWTimeout):
                    reason = f"timed out waiting: {reason}"
                shot = ex.shot(f"FAIL-step{idx}")
                result["steps"].append({"index": idx, "action": step.get("action"),
                                        "description": desc, "status": STATUS_FAIL,
                                        "detail": reason, "screenshot": shot})
                result["status"] = STATUS_FAIL
                result["error"] = f"Step {idx} ({step.get('action')}) failed: {reason}"
                result["failed_step"] = idx
                result["repro"] = build_repro(case, descs_done, desc, cfg,
                                              environment_details(cfg))
                break
        else:
            result["status"] = STATUS_PASS

        result["screenshots"] = ex.screenshots
        result["console_errors"] = ex.console_errors[:25]
        result["network_failures"] = ex.network_failures[:25]
        result["final_url"] = page.url
        page.close()
    except Exception as exc:  # environment-level problem, not a product bug
        result["status"] = STATUS_BLOCKED
        result["error"] = f"BLOCKED (harness/environment): {exc}"
        result["trace"] = traceback.format_exc()[-1500:]
    finally:
        if own_context and context is not None:
            try:
                context.close()
            except Exception:
                pass
        result["duration_ms"] = int((time.time() - started) * 1000)
        result["finished"] = now_iso()
    with lock:
        icon = {STATUS_PASS: "PASS", STATUS_FAIL: "FAIL", STATUS_BLOCKED: "BLOCK"}[result["status"]]
        log(f"  [{icon}] {case_id} {case.get('description','')[:70]} ({result['duration_ms']}ms)")
        if result["error"]:
            log(f"         {result['error'][:200]}")
    return result


def resolve_extension_id(context, log, timeout=15.0):
    """MV3: the service worker's URL carries the extension id. MV2 falls back to
    a background page. Poll briefly since worker registration is async."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for collection in ("service_workers", "background_pages"):
            items = getattr(context, collection, [])
            for item in items:
                url = getattr(item, "url", "")
                if url.startswith("chrome-extension://"):
                    return url.split("/")[2]
        time.sleep(0.4)
    return None


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="web_runner")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--only", default="", help="comma separated case ids")
    ap.add_argument("--tag", default="", help="comma separated tags")
    ap.add_argument("--suite", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--parallel", type=int, default=0)
    args = ap.parse_args(argv)

    proj = Path(args.project_dir).resolve()
    cfg = load_config(proj)
    plat = cfg.get("platform")
    plan = load_testplan(proj)
    log = Log(proj, plat)
    plan_env = load_env_file(proj / cfg.get("env_file", ".env.qa"))

    target = cfg.setdefault("target", {})
    if args.headed:
        target["headless"] = False
    headless = bool(target.get("headless", True))
    if plat == "extension":
        headless = False  # MV3 extensions do not load in headless_shell

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    tags = [s.strip() for s in args.tag.split(",") if s.strip()]
    cases = list(iter_cases(plan, only or None, tags or None, args.suite or None))
    if not cases:
        log("No cases matched the filters - nothing to run.")
        return 3

    parallel = args.parallel or int(cfg.get("parallel", 4))
    if plat == "extension":
        parallel = 1  # single shared persistent context
    shot_dir = proj / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "project": cfg.get("project"),
        "platform": plat,
        "started": now_iso(),
        "env": environment_details(cfg),
        "filters": {"only": only, "tags": tags, "suite": args.suite},
        "cases": [],
    }
    log(f"=== {cfg.get('project')} [{plat}] : {len(cases)} case(s), parallel={parallel}, "
        f"headless={headless} ===")

    lock = threading.Lock()
    t0 = time.time()

    with sync_playwright() as pw:
        browser_name = target.get("browser", "chromium")
        launch_args = target.get("launch_args", [])
        ext_ctx = ext_id = None

        if plat == "extension":
            ext_path = target.get("extension_path")
            if not ext_path or not Path(ext_path).exists():
                log(f"BLOCKED: extension_path '{ext_path}' does not exist")
                return 2
            if not (Path(ext_path) / "manifest.json").exists():
                log(f"BLOCKED: no manifest.json in {ext_path}")
                return 2
            user_data = proj / ".chrome-profile"
            user_data.mkdir(exist_ok=True)
            ext_ctx = pw.chromium.launch_persistent_context(
                str(user_data),
                headless=False,
                args=[f"--disable-extensions-except={ext_path}",
                      f"--load-extension={ext_path}",
                      "--no-first-run", "--no-default-browser-check"] + launch_args,
                viewport=target.get("viewport") or {"width": 1440, "height": 900},
                ignore_https_errors=True,
            )
            ext_id = resolve_extension_id(ext_ctx, log)
            log(f"extension id: {ext_id or 'UNRESOLVED'}")
            for suite, case in cases:
                results["cases"].append(
                    run_case(pw, cfg, plan_env, shot_dir, suite, case, log, lock,
                             ext_ctx=ext_ctx, ext_id=ext_id))
            ext_ctx.close()
        else:
            launcher = {"chromium": pw.chromium, "chrome": pw.chromium,
                        "firefox": pw.firefox, "webkit": pw.webkit}.get(browser_name, pw.chromium)
            lk = {"headless": headless, "args": launch_args}
            if browser_name == "chrome":
                lk["channel"] = "chrome"
            browser = launcher.launch(**lk)
            pw._qa_browser = browser
            if parallel <= 1:
                for suite, case in cases:
                    results["cases"].append(
                        run_case(pw, cfg, plan_env, shot_dir, suite, case, log, lock))
            else:
                # Playwright's sync API is not thread-safe across a single
                # connection, so each worker thread gets its own driver process.
                def worker(pair):
                    suite, case = pair
                    with sync_playwright() as local_pw:
                        lb = launcher_for(local_pw, browser_name).launch(**lk)
                        local_pw._qa_browser = lb
                        try:
                            return run_case(local_pw, cfg, plan_env, shot_dir,
                                            suite, case, log, lock)
                        finally:
                            lb.close()

                with ThreadPoolExecutor(max_workers=parallel) as pool:
                    futures = {pool.submit(worker, pair): pair for pair in cases}
                    for fut in as_completed(futures):
                        suite, case = futures[fut]
                        try:
                            results["cases"].append(fut.result())
                        except Exception as exc:
                            results["cases"].append({
                                "id": case.get("id"), "suite": suite.get("name", ""),
                                "description": case.get("description", ""),
                                "status": STATUS_BLOCKED,
                                "error": f"worker crashed: {exc}",
                                "steps": [], "screenshots": [], "repro": [],
                                "duration_ms": 0,
                            })
            browser.close()

    # Preserve the plan's declared order regardless of completion order.
    order = {c.get("id"): i for i, (_, c) in enumerate(cases)}
    results["cases"].sort(key=lambda r: order.get(r.get("id"), 999))
    results["finished"] = now_iso()
    results["duration_ms"] = int((time.time() - t0) * 1000)

    path = write_results(proj, results)
    from core.common import summarize
    s = summarize(results)
    log(f"=== {s['passed']}/{s['total']} passed ({s['pass_rate']}%), "
        f"{s['failed']} failed, {s['blocked']} blocked -> {path} ===")
    log.close()
    return 1 if (s["failed"] or s["blocked"]) else 0


def launcher_for(pw, name):
    return {"chromium": pw.chromium, "chrome": pw.chromium,
            "firefox": pw.firefox, "webkit": pw.webkit}.get(name, pw.chromium)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
