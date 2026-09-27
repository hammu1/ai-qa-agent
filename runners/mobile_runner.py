"""Android (UiAutomator2) + iOS (XCUITest) runner via Appium.

Emits the same results.json contract as the web runner, so one reporter serves
every platform. Mobile cases run sequentially per device by default - a single
device cannot be driven concurrently. With several devices attached, cases are
sharded across them and run in parallel, one session per device.

If the Appium server is not already listening, this runner starts one and stops
it on exit.
"""
import json
import os
import re
import shutil
import socket
import subprocess
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
    now_iso, slugify, summarize, write_results,
)

try:
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    from appium.webdriver.common.appiumby import AppiumBy
    from selenium.common.exceptions import WebDriverException
except ImportError:
    print("ERROR: Appium-Python-Client missing. Run: pip3 install --user Appium-Python-Client",
          file=sys.stderr)
    raise SystemExit(2)

try:
    from appium.options.ios import XCUITestOptions
except ImportError:
    XCUITestOptions = None


class StepFailure(Exception):
    pass


def port_open(host, port, timeout=1.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def adb_online() -> list:
    if not shutil.which("adb"):
        return []
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return []
    return [ln.split()[0] for ln in out.splitlines()[1:]
            if len(ln.split()) >= 2 and ln.split()[1] == "device"]


def start_appium(server_url: str, log) -> subprocess.Popen | None:
    """Boot an Appium server if nothing is listening. Returns the process to kill."""
    m = re.match(r"https?://([^:/]+):(\d+)", server_url)
    host, port = (m.group(1), m.group(2)) if m else ("127.0.0.1", "4723")
    if port_open(host, port):
        log(f"Appium already listening on {host}:{port}")
        return None
    if not shutil.which("appium"):
        raise StepFailure("appium server not installed (npm i -g appium)")
    log(f"Starting Appium server on {host}:{port} ...")
    proc = subprocess.Popen(
        ["appium", "--address", host, "--port", str(port), "--log-level", "error"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        if port_open(host, port):
            log("Appium server is up.")
            return proc
        if proc.poll() is not None:
            raise StepFailure("Appium server exited during startup")
        time.sleep(0.5)
    proc.terminate()
    raise StepFailure(f"Appium server did not come up on {host}:{port} within 30s")


BY_MAP = {
    "id": AppiumBy.ID,
    "resourceId": AppiumBy.ID,
    "accessibilityId": AppiumBy.ACCESSIBILITY_ID,
    "desc": AppiumBy.ACCESSIBILITY_ID,
    "xpath": AppiumBy.XPATH,
    "className": AppiumBy.CLASS_NAME,
    "uiautomator": AppiumBy.ANDROID_UIAUTOMATOR,
    "predicate": AppiumBy.IOS_PREDICATE,
    "classChain": AppiumBy.IOS_CLASS_CHAIN,
}


def build_selector(step, plat):
    """Resolve a step's targeting keys into an Appium (by, value) pair."""
    for key, by in BY_MAP.items():
        if key in step:
            return by, interpolate(str(step[key]), step.get("_env", {}))
    if "text" in step:
        txt = step["text"]
        if plat == "ios":
            return AppiumBy.IOS_PREDICATE, f'label == "{txt}" OR name == "{txt}" OR value == "{txt}"'
        # exact-or-contains match, covers most Android widget trees
        return AppiumBy.XPATH, (
            f'//*[@text="{txt}" or @content-desc="{txt}" '
            f'or contains(@text,"{txt}") or contains(@content-desc,"{txt}")]'
        )
    raise StepFailure(f"step '{step.get('action')}' has no target "
                      f"(need id/text/accessibilityId/xpath/uiautomator)")


def describe_target(step) -> str:
    for key in list(BY_MAP) + ["text"]:
        if key in step:
            return f"{key}={step[key]}"
    return "<no target>"


def describe_step(step) -> str:
    action = step.get("action", "?")
    if step.get("note"):
        return f"{action}: {step['note']}"
    if action == "tap":
        return f"Tap {describe_target(step)}"
    if action == "sendKeys":
        raw = str(step.get("value", ""))
        shown = "<secret>" if "${" in raw and any(
            s in raw.upper() for s in ("PASS", "TOKEN", "SECRET", "KEY")) else raw
        return f"Enter \"{shown}\" into {describe_target(step)}"
    if action == "swipe":
        return f"Swipe {step.get('direction', 'up')} x{step.get('times', 1)}"
    if action == "screenshot":
        return f"Capture screenshot '{step.get('name', 'shot')}'"
    if action == "back":
        return "Press device back button"
    if action == "wait":
        return f"Wait {step.get('ms', 1000)}ms"
    if action == "scrollTo":
        return f"Scroll until \"{step.get('text')}\" is visible"
    if action.startswith("expect"):
        return f"Assert {action[6:]} {describe_target(step)} {step.get('text', '')}".strip()
    return f"{action} {json.dumps({k: v for k, v in step.items() if not k.startswith('_')})}"


class MobileExecutor:
    def __init__(self, driver, cfg, env, shot_dir, case_id, log):
        self.d = driver
        self.cfg = cfg
        self.env = env
        self.plat = cfg.get("platform")
        self.shot_dir = shot_dir
        self.case_id = case_id
        self.log = log
        self.screenshots = []
        self._seq = 0

    def find(self, step, timeout):
        step = dict(step, _env=self.env)
        by, val = build_selector(step, self.plat)
        deadline = time.time() + timeout / 1000.0
        last = None
        while time.time() < deadline:
            try:
                els = self.d.find_elements(by, val)
                for el in els:
                    if el.is_displayed():
                        return el
                if els:
                    return els[0]
            except WebDriverException as exc:
                last = exc
            time.sleep(0.4)
        raise StepFailure(f"element not found: {describe_target(step)}"
                          + (f" ({str(last).splitlines()[0][:160]})" if last else ""))

    def shot(self, name) -> str:
        self._seq += 1
        fname = f"{slugify(self.case_id)}-{self._seq:02d}-{slugify(name)}.png"
        path = self.shot_dir / fname
        try:
            self.d.get_screenshot_as_file(str(path))
            self.screenshots.append(str(path))
            return str(path)
        except Exception as exc:
            self.log(f"  ! screenshot failed: {exc}")
            return ""

    def swipe_dir(self, direction, times=1):
        size = self.d.get_window_size()
        w, h = size["width"], size["height"]
        vectors = {
            "up":    (w // 2, int(h * 0.75), w // 2, int(h * 0.25)),
            "down":  (w // 2, int(h * 0.25), w // 2, int(h * 0.75)),
            "left":  (int(w * 0.85), h // 2, int(w * 0.15), h // 2),
            "right": (int(w * 0.15), h // 2, int(w * 0.85), h // 2),
        }
        vec = vectors.get(direction, vectors["up"])
        for _ in range(int(times)):
            self.d.swipe(*vec, 500)
            time.sleep(0.3)

    def run_step(self, step):
        action = step.get("action")
        timeout = int(step.get("timeout", self.cfg.get("target", {}).get("timeout", 15000)))
        d = self.d

        if action == "tap":
            self.find(step, timeout).click()
            return f"tapped {describe_target(step)}"
        if action == "tapCoords":
            d.tap([(int(step["x"]), int(step["y"]))])
            return f"tapped ({step['x']},{step['y']})"
        if action == "longPress":
            el = self.find(step, timeout)
            d.execute_script("mobile: longClickGesture",
                             {"elementId": el.id, "duration": int(step.get("ms", 1000))})
            return "long pressed"
        if action == "sendKeys":
            el = self.find(step, timeout)
            if step.get("clear", True):
                try:
                    el.clear()
                except WebDriverException:
                    pass
            el.send_keys(interpolate(str(step.get("value", "")), self.env))
            return "entered text"
        if action == "clear":
            self.find(step, timeout).clear()
            return "cleared"
        if action == "hideKeyboard":
            try:
                d.hide_keyboard()
            except WebDriverException:
                pass
            return "keyboard hidden"
        if action == "back":
            d.back()
            return "pressed back"
        if action == "home":
            d.execute_script("mobile: pressKey", {"keycode": 3})
            return "pressed home"
        if action == "swipe":
            self.swipe_dir(step.get("direction", "up"), step.get("times", 1))
            return f"swiped {step.get('direction', 'up')}"
        if action == "scrollTo":
            text = interpolate(str(step.get("text", "")), self.env)
            for _ in range(int(step.get("max_swipes", 8))):
                try:
                    probe = dict(step)
                    probe["text"] = text
                    el = self.find({"text": text, "timeout": 1200}, 1200)
                    if el.is_displayed():
                        return f"scrolled to '{text}'"
                except StepFailure:
                    self.swipe_dir(step.get("direction", "up"), 1)
            raise StepFailure(f"'{text}' not reachable after {step.get('max_swipes', 8)} swipes")
        if action == "wait":
            time.sleep(int(step.get("ms", 1000)) / 1000.0)
            return "waited"
        if action == "screenshot":
            return f"screenshot -> {self.shot(step.get('name', 'shot'))}"
        if action == "rotate":
            d.orientation = step.get("orientation", "LANDSCAPE").upper()
            return f"rotated {d.orientation}"
        if action == "launchApp":
            pkg = step.get("appPackage") or self.cfg.get("target", {}).get("appPackage")
            d.activate_app(pkg)
            return f"activated {pkg}"
        if action == "terminateApp":
            pkg = step.get("appPackage") or self.cfg.get("target", {}).get("appPackage")
            d.terminate_app(pkg)
            return f"terminated {pkg}"
        if action == "background":
            d.background_app(int(step.get("seconds", 3)))
            return "backgrounded"
        if action == "openNotifications":
            d.open_notifications()
            return "notifications opened"

        # ---- assertions -----------------------------------------------------
        if action == "expectVisible":
            el = self.find(step, timeout)
            if not el.is_displayed():
                raise StepFailure(f"{describe_target(step)} exists but is not displayed")
            return f"{describe_target(step)} visible"
        if action == "expectHidden":
            try:
                el = self.find(dict(step, timeout=min(timeout, 4000)), min(timeout, 4000))
            except StepFailure:
                return "not present (as expected)"
            if el.is_displayed():
                raise StepFailure(f"{describe_target(step)} is visible, expected hidden")
            return "hidden"
        if action == "expectText":
            want = interpolate(str(step.get("text", step.get("value", ""))), self.env)
            el = self.find(step, timeout)
            got = (el.text or el.get_attribute("content-desc") or "").strip()
            if want.lower() not in got.lower():
                raise StepFailure(f"expected '{want}' in element text, got '{got[:200]}'")
            return f"text contains '{want}'"
        if action == "expectEnabled":
            if not self.find(step, timeout).is_enabled():
                raise StepFailure(f"{describe_target(step)} is disabled")
            return "enabled"
        if action == "expectAppRunning":
            pkg = step.get("appPackage") or self.cfg.get("target", {}).get("appPackage")
            if self.plat == "android" and pkg:
                state = d.query_app_state(pkg)
                # 4 = running in foreground, 3 = running in background
                if state < 3:
                    raise StepFailure(f"app {pkg} is not running (state={state}) - likely crashed")
                return f"app running (state={state})"
            src = d.page_source or ""
            if len(src) < 50:
                raise StepFailure("no UI hierarchy returned - app may have crashed")
            return "app responsive"
        if action == "expectActivity":
            want = step.get("activity", "")
            got = getattr(d, "current_activity", "") or ""
            if want not in got:
                raise StepFailure(f"current activity '{got}', expected '{want}'")
            return f"activity {got}"
        if action == "expectNoCrash":
            src = d.page_source or ""
            for marker in ("has stopped", "keeps stopping", "isn't responding", "Application Error"):
                if marker.lower() in src.lower():
                    raise StepFailure(f"crash dialog detected: '{marker}'")
            return "no crash dialog"
        if action == "expectToast":
            want = interpolate(str(step.get("text", "")), self.env)
            deadline = time.time() + timeout / 1000.0
            while time.time() < deadline:
                if want.lower() in (d.page_source or "").lower():
                    return f"toast '{want}' seen"
                time.sleep(0.3)
            raise StepFailure(f"toast '{want}' never appeared")

        raise StepFailure(f"unknown action '{action}'")


def build_caps(cfg, device_serial=None):
    target = cfg.get("target", {})
    plat = cfg.get("platform")
    caps = {
        "platformName": "Android" if plat == "android" else "iOS",
        "newCommandTimeout": int(target.get("newCommandTimeout", 180)),
    }
    if plat == "android":
        caps["automationName"] = target.get("automationName", "UiAutomator2")
        caps["appium:udid"] = device_serial or target.get("device") or None
        if target.get("app"):
            caps["appium:app"] = str(Path(target["app"]).expanduser().resolve())
        if target.get("appPackage"):
            caps["appium:appPackage"] = target["appPackage"]
        if target.get("appActivity"):
            caps["appium:appActivity"] = target["appActivity"]
        caps["appium:noReset"] = bool(target.get("noReset", False))
        caps["appium:fullReset"] = bool(target.get("fullReset", False))
        caps["appium:autoGrantPermissions"] = bool(target.get("autoGrantPermissions", True))
        if target.get("platformVersion"):
            caps["appium:platformVersion"] = str(target["platformVersion"])
    else:
        caps["automationName"] = target.get("automationName", "XCUITest")
        for key in ("app", "bundleId", "udid", "platformVersion", "deviceName"):
            if target.get(key):
                caps[f"appium:{key}"] = str(target[key])
    caps = {k: v for k, v in caps.items() if v is not None}
    caps.update({f"appium:{k}": v for k, v in (target.get("extra_caps") or {}).items()})
    return caps


def new_driver(cfg, server_url, device_serial=None):
    caps = build_caps(cfg, device_serial)
    plat = cfg.get("platform")
    if plat == "android":
        opts = UiAutomator2Options().load_capabilities(caps)
    else:
        if XCUITestOptions is None:
            raise StepFailure("XCUITestOptions unavailable in this Appium client build")
        opts = XCUITestOptions().load_capabilities(caps)
    return webdriver.Remote(server_url, options=opts)


def build_repro(case, done, failed, cfg, device) -> list:
    target = cfg.get("target", {})
    app = target.get("app") or target.get("appPackage") or target.get("bundleId") or "<app>"
    lines = [f"1. Connect device {device or target.get('device', '<device>')} "
             f"(adb devices should show it as 'device')",
             f"2. Install/launch {app}"]
    if case.get("preconditions"):
        lines.append(f"3. Precondition: {case['preconditions']}")
    n = len(lines) + 1
    for desc in done:
        lines.append(f"{n}. {desc}")
        n += 1
    if failed:
        lines.append(f"{n}. {failed}   <-- FAILS HERE")
    return lines


def run_case(driver, cfg, env, shot_dir, suite, case, log, lock, device):
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
        "device": device,
        "status": STATUS_BLOCKED,
        "steps": [], "screenshots": [], "console_errors": [],
        "network_failures": [], "error": "", "repro": [],
        "started": now_iso(),
    }
    try:
        ex = MobileExecutor(driver, cfg, env, shot_dir, case_id, log)
        done = []
        for idx, step in enumerate(case.get("steps", []), start=1):
            desc = describe_step(step)
            try:
                detail = ex.run_step(step)
                result["steps"].append({"index": idx, "action": step.get("action"),
                                        "description": desc, "status": STATUS_PASS,
                                        "detail": str(detail)[:500]})
                done.append(desc)
            except (StepFailure, WebDriverException, AssertionError) as exc:
                reason = str(exc).split("\n")[0][:600]
                shot = ex.shot(f"FAIL-step{idx}")
                result["steps"].append({"index": idx, "action": step.get("action"),
                                        "description": desc, "status": STATUS_FAIL,
                                        "detail": reason, "screenshot": shot})
                result["status"] = STATUS_FAIL
                result["error"] = f"Step {idx} ({step.get('action')}) failed: {reason}"
                result["failed_step"] = idx
                result["repro"] = build_repro(case, done, desc, cfg, device)
                break
        else:
            result["status"] = STATUS_PASS
        result["screenshots"] = ex.screenshots
        try:
            if cfg.get("platform") == "android":
                result["final_activity"] = driver.current_activity
        except Exception:
            pass
        # Surface the app's own crash/error logs alongside the failure.
        if result["status"] == STATUS_FAIL and cfg.get("platform") == "android":
            try:
                logs = driver.get_log("logcat")[-400:]
                result["console_errors"] = [
                    l.get("message", "") for l in logs
                    if any(m in l.get("message", "") for m in
                           ("FATAL EXCEPTION", "AndroidRuntime", " E ", "ANR "))
                ][-20:]
            except Exception:
                pass
    except Exception as exc:
        result["status"] = STATUS_BLOCKED
        result["error"] = f"BLOCKED (harness/environment): {exc}"
        result["trace"] = traceback.format_exc()[-1500:]
    finally:
        result["duration_ms"] = int((time.time() - started) * 1000)
        result["finished"] = now_iso()
    with lock:
        icon = {STATUS_PASS: "PASS", STATUS_FAIL: "FAIL", STATUS_BLOCKED: "BLOCK"}[result["status"]]
        log(f"  [{icon}] {case_id} {case.get('description','')[:70]} ({result['duration_ms']}ms)")
        if result["error"]:
            log(f"         {result['error'][:200]}")
    return result


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="mobile_runner")
    ap.add_argument("--project-dir", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--suite", default="")
    ap.add_argument("--parallel", type=int, default=0, help="max devices to shard across")
    ap.add_argument("--headed", action="store_true", help="ignored on mobile")
    args = ap.parse_args(argv)

    proj = Path(args.project_dir).resolve()
    cfg = load_config(proj)
    plat = cfg.get("platform")
    plan = load_testplan(proj)
    log = Log(proj, plat)
    env = load_env_file(proj / cfg.get("env_file", ".env.qa"))
    target = cfg.setdefault("target", {})
    server_url = target.get("server", "http://127.0.0.1:4723")

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    tags = [s.strip() for s in args.tag.split(",") if s.strip()]
    cases = list(iter_cases(plan, only or None, tags or None, args.suite or None))
    if not cases:
        log("No cases matched the filters - nothing to run.")
        return 3

    shot_dir = proj / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "project": cfg.get("project"), "platform": plat, "started": now_iso(),
        "env": environment_details(cfg),
        "filters": {"only": only, "tags": tags, "suite": args.suite},
        "cases": [],
    }

    if plat == "ios" and sys.platform != "darwin":
        log("BLOCKED: iOS automation requires macOS + Xcode (XCUITest). "
            "Use a macOS runner or a cloud device farm via target.server.")
        for suite, case in cases:
            results["cases"].append({
                "id": case.get("id"), "suite": suite.get("name", ""),
                "description": case.get("description", ""),
                "type": case.get("type", "Functional"),
                "priority": case.get("priority", "P2"),
                "status": STATUS_BLOCKED,
                "error": "BLOCKED: iOS/XCUITest is unavailable on Linux hosts.",
                "steps": [], "screenshots": [], "repro": [], "duration_ms": 0,
            })
        results["finished"] = now_iso()
        write_results(proj, results)
        log.close()
        return 2

    # Decide which devices to drive.
    devices = adb_online() if plat == "android" else []
    if plat == "android":
        if target.get("device"):
            devices = [target["device"]] if target["device"] in devices else devices
        if not devices:
            log("BLOCKED: no Android device online. Attach a device (USB debugging) "
                "or `adb connect <ip>:5555`, then re-run.")
            for suite, case in cases:
                results["cases"].append({
                    "id": case.get("id"), "suite": suite.get("name", ""),
                    "description": case.get("description", ""),
                    "type": case.get("type", "Functional"),
                    "priority": case.get("priority", "P2"),
                    "status": STATUS_BLOCKED,
                    "error": "BLOCKED: no Android device/emulator online (adb devices was empty).",
                    "steps": [], "screenshots": [], "repro": [], "duration_ms": 0,
                })
            results["finished"] = now_iso()
            write_results(proj, results)
            log.close()
            return 2
    shards = max(1, min(args.parallel or len(devices) or 1, len(devices) or 1))
    devices = devices[:shards] or [target.get("device")]

    lock = threading.Lock()
    t0 = time.time()
    appium_proc = None
    try:
        appium_proc = start_appium(server_url, log)
    except StepFailure as exc:
        log(f"BLOCKED: {exc}")
        log.close()
        return 2

    log(f"=== {cfg.get('project')} [{plat}] : {len(cases)} case(s) across "
        f"{len(devices)} device(s): {', '.join(str(d) for d in devices)} ===")

    try:
        def run_shard(device, shard_cases):
            out = []
            driver = None
            try:
                driver = new_driver(cfg, server_url, device)
            except Exception as exc:
                reason = str(exc).split("\n")[0][:400]
                log(f"  [BLOCK] session failed on {device}: {reason}")
                for suite, case in shard_cases:
                    out.append({
                        "id": case.get("id"), "suite": suite.get("name", ""),
                        "description": case.get("description", ""),
                        "type": case.get("type", "Functional"),
                        "priority": case.get("priority", "P2"),
                        "status": STATUS_BLOCKED, "device": device,
                        "error": f"BLOCKED: could not start Appium session: {reason}",
                        "steps": [], "screenshots": [], "repro": [], "duration_ms": 0,
                    })
                return out
            try:
                for suite, case in shard_cases:
                    out.append(run_case(driver, cfg, env, shot_dir, suite, case,
                                        log, lock, device))
                    # Reset to a known state between cases.
                    try:
                        pkg = target.get("appPackage")
                        if pkg and cfg.get("platform") == "android":
                            driver.terminate_app(pkg)
                            driver.activate_app(pkg)
                    except Exception:
                        pass
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
            return out

        if len(devices) <= 1:
            results["cases"].extend(run_shard(devices[0], cases))
        else:
            buckets = {d: [] for d in devices}
            for i, pair in enumerate(cases):
                buckets[devices[i % len(devices)]].append(pair)
            with ThreadPoolExecutor(max_workers=len(devices)) as pool:
                futs = [pool.submit(run_shard, d, c) for d, c in buckets.items() if c]
                for fut in as_completed(futs):
                    results["cases"].extend(fut.result())
    finally:
        if appium_proc is not None:
            appium_proc.terminate()
            try:
                appium_proc.wait(timeout=10)
            except Exception:
                appium_proc.kill()
            log("Appium server stopped.")

    order = {c.get("id"): i for i, (_, c) in enumerate(cases)}
    results["cases"].sort(key=lambda r: order.get(r.get("id"), 999))
    results["finished"] = now_iso()
    results["duration_ms"] = int((time.time() - t0) * 1000)
    path = write_results(proj, results)
    s = summarize(results)
    log(f"=== {s['passed']}/{s['total']} passed ({s['pass_rate']}%), "
        f"{s['failed']} failed, {s['blocked']} blocked -> {path} ===")
    log.close()
    return 1 if (s["failed"] or s["blocked"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
