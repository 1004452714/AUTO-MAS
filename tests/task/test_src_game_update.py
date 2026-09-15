import asyncio
from pathlib import Path

import pytest

from app.task.SRC.tools import game_update
from app.task.SRC.tools.game_update import ensure_game_updated


def test_client_version_comparison() -> None:
    assert game_update.is_client_outdated("4.3.0", "4.4.0")
    assert game_update.is_client_outdated("4.3.9", "4.4.0")
    assert not game_update.is_client_outdated("4.4.0", "4.4.0")
    assert not game_update.is_client_outdated("4.4.1", "4.4.0")

    # 段数不等时按缺失段补 0 比较
    assert game_update.is_client_outdated("4.4", "4.4.1")
    assert not game_update.is_client_outdated("4.4.0", "4.4")

    # 任一侧解析不出数字时不下判断，避免误拦正常代理
    assert not game_update.is_client_outdated("unknown", "4.4.0")
    assert not game_update.is_client_outdated("4.4.0", "")


def _patch_versions(
    monkeypatch: pytest.MonkeyPatch,
    remote: str | None,
    installed: str | None,
) -> None:
    async def fake_fetch() -> str | None:
        return remote

    async def fake_installed(
        adb_path: Path | None, adb_address: str, package_name: str
    ) -> str | None:
        return installed

    monkeypatch.setattr(game_update, "fetch_game_version", fake_fetch)
    monkeypatch.setattr(game_update, "get_installed_client_version", fake_installed)


def _run(**overrides) -> game_update.GameUpdateResult:
    kwargs = {
        "adb_path": None,
        "adb_address": "127.0.0.1:16384",
        "server": "CN-Official",
        "package_name": "com.miHoYo.hkrpg",
        "apk_dir": Path("data/GameApk"),
        "if_auto_install": True,
        "time_limit": 60,
    }
    kwargs.update(overrides)
    return asyncio.run(ensure_game_updated(**kwargs))


def test_up_to_date_skips_update(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_versions(monkeypatch, "4.4.0", "4.4.0")

    result = _run()

    assert result.status == "UpToDate"


def test_non_official_server_requires_manual_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_versions(monkeypatch, "4.4.0", "4.3.0")

    result = _run(
        server="CN-Bilibili", package_name="com.miHoYo.hkrpg.bilibili"
    )

    assert result.status == "NeedManualUpdate"
    assert "仅国服官服支持自动更新" in result.message


def test_auto_install_disabled_requires_manual_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_versions(monkeypatch, "4.4.0", "4.3.0")

    result = _run(if_auto_install=False)

    assert result.status == "NeedManualUpdate"
    assert "未开启自动安装" in result.message


def test_unreadable_installed_version_does_not_block_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_versions(monkeypatch, "4.4.0", None)

    result = _run()

    assert result.status == "Skipped"


def test_missing_adb_address_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_versions(monkeypatch, "4.4.0", "4.3.0")

    assert _run(adb_address="Unknown").status == "Skipped"


def test_version_api_failure_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_versions(monkeypatch, None, "4.3.0")

    assert _run().status == "Skipped"
