import os

import pytest

from agents_history import __version__, cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HISTORY_"):
            monkeypatch.delenv(key)


def test_bad_config_exits_78(monkeypatch, capsys):
    assert cli.main(["migrate"]) == 78
    assert "HISTORY_DATABASE_URL" in capsys.readouterr().err


def test_commands_registered():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == {"migrate", "api"}


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["nope"]])
def test_unknown_command_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "history" in (captured.out + captured.err).lower()
