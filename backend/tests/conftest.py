import os

# Config (app.core.config) waliduje env przy imporcie - zapewnij minimum,
# zeby testy dzialaly takze bez pliku .env (np. CI).
os.environ.setdefault("CLAUDE_BIN_PATH", "claude")

# Mocki dev (app/core/dev_mode.py) wylaczone na sile w calym suite - tryb auto
# zmienialby semantyke testow "brak klucza" (np. pisal pliki do .dev-outbox).
# Testy mocków (test_dev_mocks.py) wlaczaja je jawnie przez monkeypatch.
os.environ.setdefault("MOCK_EMAIL", "false")
os.environ.setdefault("MOCK_SENDER", "false")
os.environ.setdefault("MOCK_CIRCLE_MEMBERS", "false")
