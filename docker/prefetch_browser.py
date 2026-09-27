"""Build step: download CloakBrowser and a ChromeDriver matching its Chromium version."""
from cloakbrowser.download import ensure_binary
from selenium.webdriver.common.selenium_manager import SeleniumManager

binary = ensure_binary()
print(f"CloakBrowser binary: {binary}")

paths = SeleniumManager().binary_paths(["--browser", "chrome", "--browser-path", binary])
print(f"ChromeDriver: {paths['driver_path']}")
