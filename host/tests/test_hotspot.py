from __future__ import annotations

import os
from pathlib import Path

import usbnic_host.monitor.hotspot as hotspot
from usbnic_host.config import Config


def _fake_nmcli(values: dict[tuple[str, ...], str]):
    def run(*args: str) -> str:
        return values.get(tuple(args), "")

    return run


def test_hotspot_info_reads_password_file(tmp_path: Path, monkeypatch) -> None:
    pass_file = tmp_path / "hotspot.pass"
    pass_file.write_text("CorrectHorse9!BatteryStaple\n", encoding="utf-8")
    pass_file.chmod(0o600)

    values = {
        ("-g", "802-11-wireless.ssid", "connection", "show", "usbnic-hotspot"): "Laptop-AP",
        ("-g", "802-11-wireless-security.key-mgmt", "connection", "show", "usbnic-hotspot"): "sae",
        ("-g", "802-11-wireless-security.pmf", "connection", "show", "usbnic-hotspot"): "3",
        ("-g", "802-11-wireless-security.proto", "connection", "show", "usbnic-hotspot"): "rsn",
        ("-g", "802-11-wireless-security.pairwise", "connection", "show", "usbnic-hotspot"): "ccmp",
        ("-g", "802-11-wireless-security.group", "connection", "show", "usbnic-hotspot"): "ccmp",
        ("-g", "connection.interface-name", "connection", "show", "usbnic-hotspot"): "wlx-test",
        ("-g", "connection.autoconnect", "connection", "show", "usbnic-hotspot"): "no",
        ("-g", "GENERAL.STATE", "device", "show", "wlx-test"): "100 (connected)",
    }
    monkeypatch.setattr(hotspot, "_nmcli", _fake_nmcli(values))

    info = hotspot.hotspot_info(Config(hotspot_password_file=str(pass_file)))

    assert info["available"] is True
    assert info["ssid"] == "Laptop-AP"
    assert info["active"] is True
    assert info["security"]["key_mgmt"] == "sae"
    assert info["security"]["pmf"] == "3"
    assert info["password"] == "CorrectHorse9!BatteryStaple"
    assert info["password_source"] == "file"
    assert info["password_file_exists"] is True


def test_hotspot_info_creates_root_only_file(tmp_path: Path, monkeypatch) -> None:
    pass_file = tmp_path / "etc" / "hotspot.pass"
    values = {
        ("-g", "802-11-wireless.ssid", "connection", "show", "usbnic-hotspot"): "Laptop-AP",
        ("-g", "connection.interface-name", "connection", "show", "usbnic-hotspot"): "wlx-test",
        ("-g", "GENERAL.STATE", "device", "show", "wlx-test"): "30 (disconnected)",
        (
            "-s",
            "-g",
            "802-11-wireless-security.psk",
            "connection",
            "show",
            "usbnic-hotspot",
        ): "Generated9!Password_value",
    }
    monkeypatch.setattr(hotspot, "_nmcli", _fake_nmcli(values))
    monkeypatch.setattr(hotspot.os, "geteuid", lambda: 0)

    info = hotspot.hotspot_info(Config(hotspot_password_file=str(pass_file)))

    assert info["password"] == "Generated9!Password_value"
    assert info["password_source"] == "file"
    assert info["password_file_exists"] is True
    assert pass_file.read_text(encoding="utf-8") == "Generated9!Password_value\n"
    assert os.stat(pass_file).st_mode & 0o777 == 0o600


def test_hotspot_info_without_connection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hotspot, "_nmcli", _fake_nmcli({}))

    info = hotspot.hotspot_info(
        Config(hotspot_password_file=str(tmp_path / "missing.pass"))
    )

    assert info["available"] is False
    assert info["password"] == ""
    assert info["active"] is False
    assert info["password_file_exists"] is False
