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

        driver.execute_script("switchTab('calendar')")
        changes_toggle = wait.until(
            expected_conditions.visibility_of_element_located((By.ID, "calendar-changes-toggle"))
        )
        changes_panel = driver.find_element(By.ID, "calendar-changes-panel")
        changes_arrow = changes_toggle.find_element(By.CLASS_NAME, "calendar-toggle-arrow")
        changes_open_transform = changes_arrow.value_of_css_property("transform")
        assert changes_toggle.get_attribute("aria-expanded") == "true"
        assert changes_panel.is_displayed()

        changes_toggle.click()
        wait.until(lambda _current: changes_toggle.get_attribute("aria-expanded") == "false")
        assert not changes_panel.is_displayed()
        wait.until(lambda _current: changes_arrow.value_of_css_property("transform") != changes_open_transform)

        quality_toggle = driver.find_element(By.ID, "calendar-gm-toggle")
        quality_legend = driver.find_element(By.CLASS_NAME, "cal-gm-legend")
        quality_arrow = quality_toggle.find_element(By.CLASS_NAME, "calendar-toggle-arrow")
        quality_closed_transform = quality_arrow.value_of_css_property("transform")
        assert quality_toggle.get_attribute("aria-pressed") == "false"
        assert not quality_legend.is_displayed()

        quality_toggle.click()
        wait.until(lambda _current: quality_toggle.get_attribute("aria-pressed") == "true")
        assert quality_legend.is_displayed()
        wait.until(lambda _current: quality_arrow.value_of_css_property("transform") != quality_closed_transform)

        categories_toggle = driver.find_element(
            By.CSS_SELECTOR,
            '[data-cal-dd="categories"] [data-cal-dd-btn]',
        )
        categories_arrow = categories_toggle.find_element(By.CLASS_NAME, "calendar-toggle-arrow")
        categories_closed_transform = categories_arrow.value_of_css_property("transform")
        categories_toggle.click()
        wait.until(lambda _current: categories_toggle.get_attribute("aria-expanded") == "true")
        wait.until(lambda _current: categories_arrow.value_of_css_property("transform") != categories_closed_transform)

        # Switching language translates current and future UI, preserving names,
        # filter values, saved language, and theme behavior across both entry pages.
        driver.get(f'http://127.0.0.1:{server.server_port}/index.html')
        language_button = wait.until(expected_conditions.visibility_of_element_located(
            (By.CSS_SELECTOR, '.home-preferences .language-toggle')
        ))
        theme_button = driver.find_element(By.ID, 'home-dark-btn')
        assert language_button.rect['x'] > theme_button.rect['x']
        assert language_button.text == 'ES'
        language_button.click()
        wait.until(lambda current: current.find_element(By.TAG_NAME, 'html').get_attribute('lang') == 'es')
        assert language_button.text == 'EN'
        assert language_button.find_element(By.TAG_NAME, 'img').get_attribute('src').endswith('/flags/gb.svg')
        assert driver.find_element(By.ID, 'home-dark-label').text == 'Modo oscuro'
        theme_button.click()
        wait.until(lambda current: current.find_element(By.ID, 'home-dark-label').text == 'Modo claro')

        driver.get(f'http://127.0.0.1:{server.server_port}/app.html#calendar')
        wait.until(lambda current: current.find_element(By.ID, 'calendar-changes-toggle').text.endswith('Cambios'))
        driver.execute_script('''
            const fixture = document.createElement('div');
            fixture.id = 'language-fixture';
            fixture.innerHTML = '<span translate="no">May</span><span class="calendar-tournament">Changes</span>'
                + '<span class="history-player-name">Clay</span><span id="dynamic-ui">Points: 123</span>'
                + '<span id="plain-date">May 31</span><table><thead><tr>'
                + '<th id="calendar-date" class="cal-week-header">September 14</th></tr></thead></table>'
                + '<span id="entry-week" class="entry-menu-week">WEEK OF SEPTEMBER 21</span>'
                + '<span id="round-result">1st + QR2</span><span class="draw-player">'
                + '<span class="name" data-ui-label id="qualifier-label">Qualifier</span></span>'
                + '<button id="seed-value" data-value="Yes">Yes</button>'
                + '<select id="language-options"><option>Grass</option></select>';
            document.body.appendChild(fixture);
        ''')
        wait.until(lambda current: current.find_element(By.ID, 'dynamic-ui').text == 'Puntos: 123')
        for selector, expected in [('[translate=no]', 'May'), ('.calendar-tournament', 'Changes'),
                                   ('.history-player-name', 'Clay')]:
            assert driver.execute_script(
                "return document.querySelector('#language-fixture ' + arguments[0]).textContent", selector
            ) == expected
        assert driver.execute_script("return document.getElementById('language-options').value") == 'Grass'
        assert driver.find_element(By.CSS_SELECTOR, '#language-options option').text == 'Césped'
        assert driver.find_element(By.ID, 'plain-date').text == 'May 31'
        assert driver.find_element(By.ID, 'calendar-date').text == '14 DE SEPTIEMBRE'
        assert driver.find_element(By.ID, 'entry-week').text == '21 DE SEPTIEMBRE'
        assert driver.find_element(By.ID, 'round-result').text == '1ª + QR2'
        assert driver.find_element(By.ID, 'qualifier-label').text == 'CLASIFICADA'
        assert driver.find_element(By.ID, 'seed-value').text == 'Si'
        assert driver.find_element(By.ID, 'seed-value').get_attribute('data-value') == 'Yes'
        driver.execute_script("document.getElementById('dynamic-ui').textContent = 'Points: 456'")
        wait.until(lambda current: current.find_element(By.ID, 'dynamic-ui').text == 'Puntos: 456')
        for source, translated in [
            ('1-50 of 120', '1-50 de 120'), ('May 17', 'May 17'),
            ('TOURNAMENT', 'TORNEO'), ('Renamed to Changes', 'Renombrado a Changes'),
            ('Surface changed to Grass', 'Superficie cambiada a Césped'), ('Losses by RET', 'Derrotas por retiro'),
            ('Round of 32', 'Dieciseisavos'), ('I.clay', 'Arcilla (cubierta)'),
            ('Draw', 'DRAW'), ('Drop Date', 'Vence'), ('Seed', 'Seed'),
            ('Cut Off', 'CORTE'), ('Acc. Pts', 'PTS ACU.'), ('Est. Need', 'EST. NEC.'),
            ('ACC. PTS', 'PTS ACU.'), ('Last week for AO MD/Q', 'Ult. Semana para AO MD/Q'),
            ('WTA Tournament Strength', 'Nivel Torneos WTA'), ('Yes', 'Si'), ('No', 'No'),
            ('Geometric Mean: Overall draw quality across all players.',
             'Media geométrica: Nivel general del cuadro considerando a todas las jugadoras.'),
        ]:
            assert driver.execute_script('return WTARG_I18N.translate(arguments[0])', source) == translated
        assert driver.execute_script(
            "return parseFloat(getComputedStyle(document.getElementById('ts-filter-surface')).width)"
        ) >= 125
        driver.execute_script("WTARG_I18N.setLanguage('en')")
        assert driver.find_element(By.ID, 'dynamic-ui').text == 'Points: 456'
        assert driver.find_element(By.CSS_SELECTOR, '#language-options option').text == 'Grass'
        assert driver.find_element(By.ID, 'calendar-date').text == 'SEPTEMBER 14'
        assert driver.find_element(By.ID, 'entry-week').text == 'SEPTEMBER 21'
        assert driver.find_element(By.ID, 'round-result').text == '1st + QR2'
        assert driver.find_element(By.ID, 'qualifier-label').text == 'Qualifier'
        assert driver.find_element(By.ID, 'seed-value').text == 'Yes'
        driver.execute_script("WTARG_I18N.setLanguage('es')")

        # Reload in Spanish, then return to English from the mobile header.
        driver.refresh()
        wait.until(lambda current: current.find_element(By.TAG_NAME, 'html').get_attribute('lang') == 'es')
        driver.set_window_size(390, 844)
        assert driver.execute_script("""
            const filters = document.querySelector('#view-tstrength .ts-row2');
            const surfaceHead = document.querySelector('#tstrength-table th:nth-child(7)');
            const historySurfaceHead = document.querySelector('#history-table th:nth-child(3)');
            return getComputedStyle(filters).display === 'grid'
                && getComputedStyle(surfaceHead, '::after').content === '"SUPERF."'
                && getComputedStyle(historySurfaceHead, '::after').content === '"SPF"';
        """)
        mobile_language = wait.until(expected_conditions.visibility_of_element_located(
            (By.CSS_SELECTOR, '.mobile-app-header .language-toggle')
        ))
        mobile_theme = driver.find_element(By.CSS_SELECTOR, '.mobile-header-theme')
        assert mobile_language.rect['x'] + mobile_language.rect['width'] <= mobile_theme.rect['x']
        mobile_language.click()
        wait.until(lambda current: current.find_element(By.ID, 'calendar-changes-toggle').text.endswith('Changes'))
        driver.get(f'http://127.0.0.1:{server.server_port}/index.html')
        home_language = wait.until(expected_conditions.visibility_of_element_located(
            (By.CSS_SELECTOR, '.home-preferences .language-toggle')
        ))
        assert home_language.text == 'ES'
        home_language.click()
        wait.until(lambda current: current.find_element(By.TAG_NAME, 'html').get_attribute('lang') == 'es')
        wait.until(lambda current: current.find_element(
            By.CSS_SELECTOR, '.home-btn[href*="#calendar"] .home-label'
        ).text == 'Calendario')
        driver.find_element(By.CSS_SELECTOR, '.home-btn').click()
        wait.until(lambda current: current.find_element(By.TAG_NAME, 'html').get_attribute('lang') == 'es')

        messages = "\n".join(entry["message"] for entry in driver.get_log("browser"))
        assert "Uncaught" not in messages
    finally:
        driver.quit()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
