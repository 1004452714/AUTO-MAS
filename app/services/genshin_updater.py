#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team

#   This file is part of AUTO-MAS.

#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.

#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

"""原神客户端更新的原神门面。

编排在 :mod:`app.services.gi_updater.pipeline`——挪出事件循环、把进度转成调度台日志、
混装目录 / 只应用增量包 / 磁盘余量三道门禁都在那里，与具体游戏无关。本模块只做一件事：
把原神的短名钉进入口，并沿用宿主既有的函数名与结论类型。

接一款新游戏时照抄本文件、换掉短名即可，不必再复制一遍编排。
"""

from __future__ import annotations

from pathlib import Path

from app.services.gi_updater.pipeline import (
    AbortHook,
    ProgressHook,
    UpdateResult,
    detect_region,
    update_client,
)
from app.services.gi_updater.presets import GameKey

__all__ = ["GenshinUpdateResult", "detect_genshin_region", "update_genshin_client"]

#: 一轮原神客户端更新的结论——类型与名字沿用宿主既有调用方
GenshinUpdateResult = UpdateResult


def detect_genshin_region(game_exe: str) -> str:
    """按游戏程序文件名判定区服，供 BetterGI 专项宿主复用。

    Args:
        game_exe: 「游戏程序」里选的可执行文件路径；手改配置时可能带
            包裹引号，所以先清洗再取名比对。

    Returns:
        ``"cn"`` / ``"global"``；不是原神游戏程序时返回空串。
    """
    return detect_region(GameKey.Genshin, game_exe)


async def update_genshin_client(
    game_path: str | Path,
    *,
    resource: str = "自动",
    time_limit_min: int = 0,
    on_progress: ProgressHook | None = None,
    should_abort: AbortHook | None = None,
) -> GenshinUpdateResult:
    """检查并按需更新原神客户端，直到落盘完成。

    Args:
        game_path: 游戏安装目录（``config.ini`` 与可执行文件所在的那一级）。
        resource: 区服，``自动`` / ``官服`` / ``国际服``。
        time_limit_min: 本轮时限（分钟）；``0`` 表示不限。超时按中止处理，
            等下载线程真收干净了才返回。
        on_progress: 一行行进度文案的回调。
        should_abort: 外部中止判定（如用户按了停止）。

    Returns:
        :class:`GenshinUpdateResult`。任何异常都转成 ``success=False`` 的结论，
        不把 traceback 抛给调度侧。
    """
    return await update_client(
        GameKey.Genshin,
        game_path,
        resource=resource,
        time_limit_min=time_limit_min,
        on_progress=on_progress,
        should_abort=should_abort,
    )
