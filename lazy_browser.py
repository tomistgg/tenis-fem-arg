class LazyBrowserSession:
    """Create a browser only when a caller first uses browser functionality."""

    def __init__(self, factory):
        self._factory = factory
        self._driver = None

    @property
    def started(self):
        return self._driver is not None

    def _ensure_started(self):
        if self._driver is None:
            self._driver = self._factory()
        return self._driver

    def __getattr__(self, name):
        return getattr(self._ensure_started(), name)

    def quit(self):
        if self._driver is None:
            return
        driver, self._driver = self._driver, None
        driver.quit()

    def reset(self):
        """Close an existing browser without starting a replacement eagerly."""
        self.quit()
