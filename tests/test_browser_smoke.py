import functools
import http.server
import os
import shutil
import threading
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait


def _first_existing(paths):
    return next((str(path) for path in paths if path and Path(path).is_file()), None)


def _browser_paths():
    driver = os.environ.get("CHROMEDRIVER") or shutil.which("chromedriver")
    browser = os.environ.get("CHROME_BINARY")
    if not browser:
        browser = next(
            (
                shutil.which(name)
                for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                if shutil.which(name)
            ),
            None,
        )
    if not browser and os.name == "nt":
        browser = _first_existing(
            [
                Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            ]
        )
    return browser, driver


@pytest.mark.browser
def test_generated_site_loads_rankings_bundle_offline(offline_generated_site):
    browser_binary, driver_binary = _browser_paths()
    required = os.environ.get("WTARG_REQUIRE_BROWSER_TESTS") == "1"
    if not browser_binary or not driver_binary:
        message = f"Chrome browser/driver not available (browser={browser_binary!r}, driver={driver_binary!r})"
        if required:
            pytest.fail(message)
        pytest.skip(message)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(offline_generated_site))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    options = Options()
    options.binary_location = browser_binary
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--host-resolver-rules=MAP * 127.0.0.1, EXCLUDE localhost")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
    driver = webdriver.Chrome(service=Service(driver_binary), options=options)
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": ["https://*"]})
        driver.get(f"http://127.0.0.1:{server.server_port}/app.html#rankings")
        wait = WebDriverWait(driver, 15)
        wait.until(expected_conditions.presence_of_element_located((By.ID, "rankings-table")))
        wait.until(lambda current: current.execute_script("return document.readyState") == "complete")
        assert "WTARG" in driver.title
        body_text = driver.find_element(By.TAG_NAME, "body").text
        assert "Failed to load local rankings data" not in body_text
        messages = "\n".join(entry["message"] for entry in driver.get_log("browser"))
        assert "Uncaught" not in messages
    finally:
        driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
