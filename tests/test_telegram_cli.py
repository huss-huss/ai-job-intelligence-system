"""telegram-setup / telegram-test against the fake Bot API."""

from app.telegram import cli

from tests.tracker_fixtures import SECRET, settings


def test_setup_sets_webhook_with_secret_and_commands(fake_tg, capsys):
    code = cli.setup("https://api.example.com/", settings=settings(), client_factory=lambda s: fake_tg.client())
    assert code == 0
    assert fake_tg.methods() == ["getMe", "setWebhook", "setMyCommands"]
    hook = fake_tg.sent("setWebhook")[0]
    assert hook["url"] == "https://api.example.com/api/telegram/webhook"
    assert hook["secret_token"] == SECRET and hook["drop_pending_updates"] is True
    commands = [c["command"] for c in fake_tg.sent("setMyCommands")[0]["commands"]]
    assert {"top", "today", "applied", "stats", "pause", "resume", "help"} <= set(commands)
    assert "@FirstFetchTestBot" in capsys.readouterr().out


def test_setup_validates_before_calling_telegram(fake_tg, capsys):
    factory = lambda s: fake_tg.client()  # noqa: E731
    assert cli.setup("http://insecure.example.com", settings=settings(), client_factory=factory) == 2
    assert (
        cli.setup("https://ok.example.com", settings=settings(webhook_secret="has spaces"), client_factory=factory) == 2
    )
    assert cli.setup("https://ok.example.com", settings=settings(bot_token=""), client_factory=factory) == 2
    assert fake_tg.requests == []
    assert "TELEGRAM_WEBHOOK_SECRET" in capsys.readouterr().err


def test_setup_without_chat_id_explains_the_next_step(fake_tg, capsys):
    cli.setup("https://api.example.com", settings=settings(chat_id=""), client_factory=lambda s: fake_tg.client())
    assert "send /start" in capsys.readouterr().out


def test_send_test_message(fake_tg):
    assert cli.send_test(settings=settings(), client_factory=lambda s: fake_tg.client()) == 0
    (body,) = fake_tg.sent()
    assert body["chat_id"] == "424242" and "test message" in body["text"]
    assert cli.send_test(settings=settings(chat_id=""), client_factory=lambda s: fake_tg.client()) == 2


def test_telegram_errors_exit_nonzero(fake_tg):
    fake_tg.fail_all = True
    assert cli.send_test(settings=settings(), client_factory=lambda s: fake_tg.client()) == 1


def test_worker_cli_exposes_the_commands():
    from worker import cli as worker_cli

    for command in ("telegram-setup", "telegram-test", "telegram-digest"):
        try:
            worker_cli.main([command, "--help"])
        except SystemExit as exc:
            assert exc.code == 0
