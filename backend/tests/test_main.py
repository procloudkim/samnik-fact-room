from samnik_backend import main


def test_console_entrypoint_is_loopback_only(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(main.uvicorn, "run", fake_run)
    main.run()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8000
