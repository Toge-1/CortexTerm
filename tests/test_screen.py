from cortexterm.tui import screen


def test_windows_empty_term_still_uses_interactive_screen(monkeypatch) -> None:
    monkeypatch.setattr(screen.sys, "platform", "win32")
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("CORTEXTERM_NO_ALT_SCREEN", raising=False)

    assert screen._is_dumb_terminal() is False


def test_no_alt_screen_env_disables_interactive_screen(monkeypatch) -> None:
    monkeypatch.setattr(screen.sys, "platform", "win32")
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("CORTEXTERM_NO_ALT_SCREEN", "1")

    assert screen._is_dumb_terminal() is True
