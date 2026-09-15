#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team

#   This file is part of AUTO-MAS.

#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.

#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty
#   of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
#   the GNU Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

#   Contact: DLmaster_361@163.com

"""模拟器层面接管崩坏·星穹铁道（国服官服）客户端更新。

SRC 拉起游戏前由 MAS 负责登录，客户端 APK 版本落后时游戏会弹出强制更新门，
登录流程会一直卡住。本模块在启动模拟器后、登录前比对版本，
官服可直接下载安装包并通过 adb 安装（先例见 ``app/task/MAA/tools/game_update.py``）。

渠道服（B 服）与国际服没有公开的版本接口和官方安卓直链，一律提示手动更新。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from app.utils import get_logger
from app.utils.constants import (
    STARRAIL_OFFICIAL_APK_URL,
    STARRAIL_VERSION_API_URL,
)
from app.utils.game_apk import (
    GameUpdateResult,
    download_apk,
    get_installed_client_version,
    install_apk,
    is_client_outdated,
)

logger = get_logger("SRC 游戏更新")

_OFFICIAL_SERVER = "CN-Official"

__all__ = ["ensure_game_updated", "fetch_game_version"]


async def fetch_game_version() -> str | None:
    """拉取国服当前客户端版本号。

    Returns:
        str | None: 版本号（形如 ``4.4.0``）；请求失败时返回 ``None``。
    """

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(STARRAIL_VERSION_API_URL, timeout=15.0)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.warning(f"获取崩坏·星穹铁道版本失败: {e}")
        return None

    try:
        game_packages = data["data"]["game_packages"]
        version = str(game_packages[0]["main"]["major"]["version"]).strip()
    except (KeyError, IndexError, TypeError):
        logger.warning(f"版本接口返回了无法解析的结构: {data}")
        return None

    if not version:
        logger.warning(f"版本接口未返回客户端版本号: {data}")
        return None

    logger.info(f"崩坏·星穹铁道国服当前版本: {version}")
    return version


async def ensure_game_updated(
    *,
    adb_path: Path | None,
    adb_address: str,
    server: str,
    package_name: str,
    apk_dir: Path,
    if_auto_install: bool,
    time_limit: int,
    progress: Callable[[str], Awaitable[None]] | None = None,
) -> GameUpdateResult:
    """在登录游戏前确认客户端版本，必要时接管更新。

    Args:
        adb_path: 模拟器自带的 adb 路径；``None`` 时回退到系统 adb。
        adb_address: 模拟器的 adb 连接地址。
        server: 用户配置的游戏服务器标识。
        package_name: 游戏包名。
        apk_dir: 安装包下载目录。
        if_auto_install: 是否允许 MAS 自动下载并安装安装包。
        time_limit: 下载与安装的超时限制（分钟）。
        progress: 进度回调，用于向前端播报当前阶段。

    Returns:
        GameUpdateResult: 检查结果；``NeedManualUpdate`` 表示本次不应继续登录与代理。
    """

    if adb_address in ("", "Unknown"):
        logger.warning("未取到模拟器 adb 地址，跳过游戏版本检查")
        return GameUpdateResult("Skipped", "未取到模拟器 adb 地址，跳过游戏版本检查")

    remote = await fetch_game_version()
    if remote is None:
        return GameUpdateResult("Skipped", "版本接口不可用，跳过游戏版本检查")

    installed = await get_installed_client_version(adb_path, adb_address, package_name)
    if installed is None:
        # 读不到已安装版本可能是游戏未安装，也可能是 adb 临时异常，
        # 一律不阻断本次代理，交回原有登录流程判定
        return GameUpdateResult("Skipped", "未能读取模拟器内的游戏版本，跳过更新检查")

    if not is_client_outdated(installed, remote):
        return GameUpdateResult("UpToDate", f"游戏客户端已是最新版本 {installed}")

    outdated_text = f"游戏客户端版本落后（已安装 {installed}，最新 {remote}）"
    logger.info(outdated_text)

    if server != _OFFICIAL_SERVER:
        return GameUpdateResult(
            "NeedManualUpdate",
            f"{outdated_text}，当前仅国服官服支持自动更新，请手动更新游戏后重试",
        )

    if not if_auto_install:
        return GameUpdateResult(
            "NeedManualUpdate",
            f"{outdated_text}，未开启自动安装，请手动更新游戏后重试",
        )

    apk_path = apk_dir / f"hsr-official-{remote}.apk"
    try:
        if progress is not None:
            await progress(f"{outdated_text}\n正在下载游戏安装包")
        await download_apk(STARRAIL_OFFICIAL_APK_URL, apk_path, progress)

        if progress is not None:
            await progress(f"{outdated_text}\n正在安装游戏安装包")
        await install_apk(adb_path, adb_address, apk_path, timeout=time_limit * 60)
    except Exception as e:
        logger.opt(exception=True).warning(f"接管游戏更新失败: {e}")
        return GameUpdateResult(
            "NeedManualUpdate",
            f"{outdated_text}，MAS 自动更新失败（{e}），请手动更新游戏后重试",
        )
    finally:
        # 安装包体积很大，无论成败都不长期占用磁盘
        apk_path.unlink(missing_ok=True)

    current = await get_installed_client_version(adb_path, adb_address, package_name)
    if current is None or is_client_outdated(current, remote):
        return GameUpdateResult(
            "NeedManualUpdate",
            f"安装后版本仍未达到 {remote}（当前 {current or '未知'}），"
            "请手动更新游戏后重试",
        )

    return GameUpdateResult("Updated", f"MAS 已将游戏客户端更新至 {current}")
