from __future__ import annotations

from pathlib import Path

from usbnic_host.api import _install_error
from usbnic_host.config import Config
from usbnic_host.monitor import rule_provider


def test_payload_lines_only_ipv4_and_dedup() -> None:
    lines = rule_provider.payload_lines(
        ["10.10.10.110", "10.10.10.110", "10.10.10.247/32", "fd00::1", "not-an-ip"]
    )
    assert lines == ["SRC-IP-CIDR,10.10.10.110/32", "SRC-IP-CIDR,10.10.10.247/32"]


def test_render_empty_payload() -> None:
    assert rule_provider.render([]) == "payload: []\n"


def test_render_payload_block() -> None:
    text = rule_provider.render(["10.10.10.110"])
    assert text == "payload:\n  - SRC-IP-CIDR,10.10.10.110/32\n"


def test_sync_skipped_without_ruleset_dir() -> None:
    result = rule_provider.sync(Config(), ["10.10.10.110"])
    assert result["ok"] is True
    assert result["skipped"] is True


def test_sync_writes_file_and_reloads(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_reload(config: Config) -> dict:
        calls.append(["reload"])
        return {"ok": True, "rules": 1, "detail": ""}

    monkeypatch.setattr(rule_provider, "reload", fake_reload)
    config = Config(clash_ruleset_dir=str(tmp_path))
    result = rule_provider.sync(config, ["10.10.10.110"])
    assert result["ok"] is True
    assert calls == [["reload"]]
    target = tmp_path / rule_provider.PROVIDER_FILE
    assert target.read_text(encoding="utf-8") == (
        "payload:\n  - SRC-IP-CIDR,10.10.10.110/32\n"
    )

    # 清单没变时不再打扰 mihomo
    again = rule_provider.sync(config, ["10.10.10.110"])
    assert again["skipped"] is True
    assert len(calls) == 1

    # 清空必须重新下发，否则残留规则会把设备继续拉去代理
    cleared = rule_provider.sync(config, [])
    assert cleared["ok"] is True
    assert target.read_text(encoding="utf-8") == "payload: []\n"
    assert len(calls) == 2


def test_install_error_names_failed_step() -> None:
    message = _install_error(
        {
            "ok": False,
            "detail": "7896 未监听",
            "steps": [
                {"name": "clash", "ok": True},
                {"name": "listener", "ok": False, "detail": "7896 未监听"},
            ],
        }
    )
    assert "listener" in message
    assert "7896 未监听" in message
