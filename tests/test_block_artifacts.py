# -*- coding: utf-8 -*-
"""Снимки блокировок с ротацией и сброс профиля браузера."""
import json
import os

from app.core.config import settings
from app.core.exceptions import AntibotBlockedError, PageLoadError
from app.marketplaces.ozon.service import BLOCK_PREFIX, OzonService


class FakeParser:
    def block_snapshot(self):
        return {"title": "Antibot Captcha", "url": "https://www.ozon.ru/x", "png": b"png"}


def test_block_artifacts_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path))
    service = OzonService(use_session=True)
    service.policy.block_artifacts_keep = 3
    for _ in range(5):
        service._save_block_artifacts(FakeParser(), AntibotBlockedError("blk"), "u")
    names = sorted(os.listdir(tmp_path))
    assert len([n for n in names if n.endswith(".json")]) == 3
    assert len([n for n in names if n.endswith(".png")]) == 3
    assert all(n.startswith(BLOCK_PREFIX) for n in names)


def test_block_artifact_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "debug_dir", str(tmp_path))
    service = OzonService(use_session=True)
    service._save_block_artifacts(FakeParser(), PageLoadError("load"), "u1")
    service._save_block_artifacts(FakeParser(), AntibotBlockedError("ban"), "u2")
    infos = []
    for n in sorted(os.listdir(tmp_path)):
        if n.endswith(".json"):
            with open(tmp_path / n, encoding="utf-8") as f:
                infos.append(json.load(f))
    assert [i["kind"] for i in infos] == ["load", "ban"]
    assert infos[1]["title"] == "Antibot Captcha"


def test_reset_profile_clears_dir(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"x")
    (profile / "Local State").write_text("{}")
    monkeypatch.setenv("OZON_CHROME_PROFILE_DIR", str(profile))
    OzonService(use_session=True).reset_profile()
    assert profile.is_dir()
    assert os.listdir(profile) == []