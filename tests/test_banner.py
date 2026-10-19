from smtppy import banner
from smtppy.banner import animate_header, build_screen, header_ansi, play_banner, strip_ansi


def test_build_screen_branding():
    screen = strip_ansi(build_screen(100, 0, 0, 15))
    assert "smtp-py" in screen
    assert "smpt-py" not in screen
    assert "BYTEROUTINE" in screen
    assert "PREVIEW MODE" in screen


def test_build_screen_delivery_stages():
    screen = strip_ansi(build_screen(100, 0, 14, 15))
    for stage in ("CONNECT", "AUTH", "COMPOSE", "DELIVER", "VERIFY"):
        assert stage in screen


def test_build_screen_fixed_frame_size():
    first = build_screen(80, 0, 0, 15).split("\n")
    for step in (3, 7, 11, 14):
        assert len(build_screen(80, 0, step, 15).split("\n")) == len(first)


def test_build_screen_narrow_terminal():
    screen = strip_ansi(build_screen(60, 0, 7, 15))
    assert "smtp-py" in screen
    for line in screen.split("\n"):
        assert len(line) <= 62


def test_header_markup_branding():
    plain = strip_ansi(header_ansi(80))
    assert "smtp-py" in plain
    assert "ByteRoutine" in plain
    assert "MAIL RELAY" not in plain


def test_play_banner_silent_without_tty(capsys):
    play_banner()
    assert capsys.readouterr().out == ""


def test_play_banner_respects_disable_flag(monkeypatch, capsys):
    monkeypatch.setenv("SMTPY_NO_BANNER", "1")
    play_banner()
    animate_header(None, 80)
    assert capsys.readouterr().out == ""
    assert banner.__name__ == "smtppy.banner"
