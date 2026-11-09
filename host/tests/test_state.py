from __future__ import annotations

import json

from usbnic_host import state


def test_save_load_roundtrip(tmp_path) -> None:
    saved = state.save(tmp_path, "gateway", True, explicit=True)
    assert saved and saved["mode"] == "gateway"
    got = state.load(tmp_path)
    assert got is not None
    assert got["mode"] == "gateway"
    assert got["proxy"] is True
    assert got["proxy_explicit"] is True


def test_default_proxy_is_not_explicit(tmp_path) -> None:
    state.save(tmp_path, "hotspot", True)
    got = state.load(tmp_path)
    assert got is not None
    assert got["proxy"] is True
    assert got["proxy_explicit"] is False


def test_missing_broken_or_invalid(tmp_path) -> None:
    assert state.load(tmp_path) is None

    (tmp_path / state.STATE_NAME).write_text("{not json", encoding="utf-8")
    assert state.load(tmp_path) is None

    (tmp_path / state.STATE_NAME).write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
    assert state.load(tmp_path) is None


def test_reject_unknown_mode(tmp_path) -> None:
    assert state.save(tmp_path, "bogus", False) is None
    assert not (tmp_path / state.STATE_NAME).exists()
