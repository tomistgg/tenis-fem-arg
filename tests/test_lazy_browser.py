from lazy_browser import LazyBrowserSession


def test_lazy_browser_does_not_start_for_quit_or_reset():
    created = []
    browser = LazyBrowserSession(lambda: created.append(object()) or created[-1])

    browser.quit()
    browser.reset()

    assert created == []
    assert browser.started is False


def test_lazy_browser_starts_on_first_use_and_can_restart_after_reset():
    created = []

    class FakeDriver:
        def __init__(self):
            self.urls = []
            self.quit_count = 0

        def get(self, url):
            self.urls.append(url)

        def quit(self):
            self.quit_count += 1

    def factory():
        driver = FakeDriver()
        created.append(driver)
        return driver

    browser = LazyBrowserSession(factory)
    browser.get("https://www.itftennis.com/")
    browser.get("https://www.itftennis.com/tennis/api/")

    assert len(created) == 1
    assert created[0].urls == ["https://www.itftennis.com/", "https://www.itftennis.com/tennis/api/"]
    assert browser.started is True

    browser.reset()
    assert created[0].quit_count == 1
    assert browser.started is False

    browser.get("https://www.itftennis.com/en/")
    assert len(created) == 2
