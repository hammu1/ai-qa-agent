"""Capability detection per platform.

The router calls this BEFORE offering a platform so the menu reflects what this
machine can actually do. A platform that cannot run is reported as blocked with
the exact remediation command - never silently degraded into fake passes.
"""
import json
import os
import platform as pyplatform
import shutil
import subprocess
import sys
from pathlib import Path

READY = "READY"
PARTIAL = "PARTIAL"
BLOCKED = "BLOCKED"

ICON = {READY: "OK ", PARTIAL: "!! ", BLOCKED: "XX "}


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _pymod(name: str):
    try:
        __import__(name)
        return True
    except Exception:
        return False


def _run(args, timeout=20):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def adb_devices():
    """Return list of (serial, state) for attached Android devices."""
    if not _has("adb"):
        return []
    rc, out, _ = _run(["adb", "devices"])
    if rc != 0:
        return []
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def playwright_browsers():
    cache = Path.home() / ".cache" / "ms-playwright"
    if not cache.exists():
        return []
    return sorted(
        d.name for d in cache.iterdir()
        if d.is_dir() and not d.name.startswith(".") and len(d.name) > 2
    )


def check_reporting() -> dict:
    checks, notes = {}, []
    checks["weasyprint (PDF)"] = _pymod("weasyprint")
    checks["openpyxl (Excel)"] = _pymod("openpyxl")
    checks["markdown"] = _pymod("markdown")
    if not checks["weasyprint (PDF)"]:
        notes.append("PDF export blocked -> pip3 install --user weasyprint (HTML/MD still produced)")
    if not checks["openpyxl (Excel)"]:
        notes.append("Excel export blocked -> pip3 install --user openpyxl (Markdown still produced)")
    status = READY if all(checks.values()) else PARTIAL
    return {"name": "Reporting", "status": status, "checks": checks, "notes": notes}


def check_web() -> dict:
    checks, notes = {}, []
    checks["python playwright"] = _pymod("playwright")
    browsers = playwright_browsers()
    checks["chromium bundle"] = any(b.startswith("chromium-") for b in browsers)
    checks["firefox bundle"] = any(b.startswith("firefox-") for b in browsers)
    checks["system chrome"] = _has("google-chrome") or _has("chromium")
    if not checks["python playwright"]:
        notes.append("pip3 install --user playwright && python3 -m playwright install chromium")
        status = BLOCKED
    elif not (checks["chromium bundle"] or checks["system chrome"]):
        notes.append("python3 -m playwright install chromium")
        status = BLOCKED
    else:
        status = READY
        if not checks["firefox bundle"]:
            notes.append("Firefox cross-browser runs need: python3 -m playwright install firefox")
    return {"name": "Web app", "status": status, "checks": checks,
            "notes": notes, "detail": f"browsers: {', '.join(browsers) or 'none'}"}


def check_extension() -> dict:
    web = check_web()
    checks = dict(web["checks"])
    notes = list(web["notes"])
    # Extensions require a real Chromium persistent context; headless_shell cannot
    # load MV3 service workers, so a headful-capable chromium is mandatory.
    checks["chromium for MV3"] = checks.get("chromium bundle") or checks.get("system chrome")
    checks["xvfb (headless CI)"] = _has("xvfb-run")
    status = web["status"]
    if status == READY and not checks["xvfb (headless CI)"]:
        notes.append("Extensions need a display; install xvfb for headless runs: sudo apt install xvfb")
        status = PARTIAL
    if not checks["chromium for MV3"]:
        status = BLOCKED
        notes.append("Extension testing needs Chromium/Chrome: python3 -m playwright install chromium")
    return {"name": "Browser extension", "status": status, "checks": checks, "notes": notes}


def check_android() -> dict:
    checks, notes = {}, []
    checks["adb"] = _has("adb")
    checks["appium server"] = _has("appium")
    checks["Appium-Python-Client"] = _pymod("appium")
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
    checks["ANDROID_HOME"] = bool(sdk) and Path(sdk).exists()

    driver_ok = False
    if checks["appium server"]:
        rc, out, err = _run(["appium", "driver", "list", "--installed"], timeout=45)
        driver_ok = "uiautomator2" in (out + err)
    checks["uiautomator2 driver"] = driver_ok

    devices = adb_devices()
    online = [d for d, s in devices if s == "device"]
    checks["device/emulator online"] = bool(online)
    checks["emulator package"] = bool(sdk) and (Path(sdk) / "emulator").exists()

    if not (checks["adb"] and checks["appium server"] and checks["Appium-Python-Client"] and driver_ok):
        status = BLOCKED
        if not checks["Appium-Python-Client"]:
            notes.append("pip3 install --user Appium-Python-Client")
        if not checks["appium server"]:
            notes.append("npm i -g appium")
        if not driver_ok:
            notes.append("appium driver install uiautomator2")
    elif not online:
        # Toolchain is complete but there is nothing to drive.
        status = PARTIAL
        notes.append("No device online. Plug in a phone with USB debugging, or run: adb connect <ip>:5555")
        if not checks["emulator package"]:
            notes.append("No emulator installed either -> sdkmanager 'emulator' 'system-images;android-34;google_apis;x86_64' then create an AVD")
    else:
        status = READY

    detail = ", ".join(f"{d}({s})" for d, s in devices) if devices else "no devices attached"
    return {"name": "Android", "status": status, "checks": checks, "notes": notes,
            "detail": f"devices: {detail}"}


def check_ios() -> dict:
    # Hard platform gate: iOS automation requires Xcode + XCUITest, macOS only.
    is_mac = pyplatform.system() == "Darwin"
    checks = {
        "macOS host": is_mac,
        "xcrun / Xcode": _has("xcrun"),
        "appium server": _has("appium"),
    }
    if not is_mac:
        return {
            "name": "iOS",
            "status": BLOCKED,
            "checks": checks,
            "notes": [
                "iOS automation is impossible on Linux - XCUITest requires macOS + Xcode.",
                "Options: a macOS runner/CI host, or a cloud device farm "
                "(BrowserStack / Sauce Labs / LambdaTest) driven over Appium's remote URL.",
            ],
            "detail": f"host is {pyplatform.system()}, not Darwin",
        }
    status = READY if all(checks.values()) else PARTIAL
    return {"name": "iOS", "status": status, "checks": checks, "notes": []}


CHECKS = {
    "web": check_web,
    "extension": check_extension,
    "android": check_android,
    "ios": check_ios,
}


def check(platform_name: str) -> dict:
    fn = CHECKS.get(platform_name)
    if not fn:
        return {"name": platform_name, "status": BLOCKED, "checks": {},
                "notes": [f"Unknown platform '{platform_name}'"]}
    return fn()


def check_all() -> dict:
    return {key: fn() for key, fn in CHECKS.items()}


def render(report: dict) -> str:
    lines = []
    for res in report.values():
        lines.append(f"{ICON[res['status']]} {res['name']:<20} {res['status']}")
        if res.get("detail"):
            lines.append(f"      {res['detail']}")
        for name, ok in res.get("checks", {}).items():
            lines.append(f"        [{'x' if ok else ' '}] {name}")
        for note in res.get("notes", []):
            lines.append(f"        -> {note}")
        lines.append("")
    return "\n".join(lines)


def main(argv):
    as_json = "--json" in argv
    targets = [a for a in argv if not a.startswith("-")]
    report = {t: check(t) for t in targets} if targets else check_all()
    rep = check_reporting()
    if as_json:
        report["reporting"] = rep
        print(json.dumps(report, indent=2))
        return 0
    print("\n=== QA Agent capability report ===\n")
    print(render(report))
    print(f"{ICON[rep['status']]} {rep['name']:<20} {rep['status']}")
    for name, ok in rep["checks"].items():
        print(f"        [{'x' if ok else ' '}] {name}")
    for note in rep["notes"]:
        print(f"        -> {note}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
