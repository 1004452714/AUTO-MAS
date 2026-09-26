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

"""BetterGI 启动前的原神客户端更新。

原神客户端由 BetterGI 自己启停，MAS 这边只经 ``game_info`` 拿到生效的游戏程序路径
（用户级 ``Switch.GamePath`` 优先，否则透传 BGI 全局配置），**不读 BGI 的其他私有
状态**。

本文件只做「取生效路径 → 判渠道 → 推调度台 → 决定是否继续任务」，查版本、下载、校验
与落盘都在 :mod:`app.services.genshin_updater`。**更新过程的所有叙述都由编排层写进
app.log**，这里只把影响本轮任务命运的少数几行推到调度台，免得刷掉专项自己的任务日志。

行为策略：

- **开关没开就什么都不做**，连日志都不写——否则每次无关任务都要刷一行；
- **查不到就放行**：路径解析不出、渠道认不出属于「无法判定」，旧客户端通常仍能登录，
  不该因为查不到版本就拦住用户；B 服与游戏正在运行同理，跳过而不是判失败；
- **需要更新却更新失败则阻断**：拿旧客户端撞登录只会白跑一轮，用户要先看一眼原因。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from app.models.config import BetterGIConfig, BetterGIUserConfig
from app.services.genshin_updater import update_genshin_client
from app.task.BetterGI.tools import game_info
from app.utils import get_logger

logger = get_logger("原神更新 BetterGI")

__all__ = ["LogHook", "ensure_game_updated", "task_stopped"]

#: 一行面向用户的进度文案
LogHook = Callable[[str], Awaitable[None]]

#: 中止判定；为真时更新在文件与数据块边界收工
AbortHook = Callable[[], bool]


def task_stopped(task: object) -> bool:
    """用户是否已要求停止本任务（供更新途中在边界轮询）。

    ``stopped_manually`` 覆盖「停止落在任务主体里」，根任务已被取消则覆盖「停止落在
    更新途中」——两个都看，用户按下停止后才不必等一次几 GB 的下载走完。口径与 HSR 的
    ``_update_aborted`` 一致。

    Args:
        task: 发起更新的任务对象。

    Returns:
        已要求停止时为真。
    """
    if bool(getattr(task, "stopped_manually", False)):
        return True
    task_info = getattr(task, "task_info", None)
    root_task = getattr(task_info, "task", None) if task_info is not None else None
    return bool(root_task is not None and root_task.cancelled())


#: 客户端渠道 -> 更新接口区服；不在表里的渠道不接管
_CHANNEL_TO_REGION: dict[str, str] = {
    game_info.CHANNEL_OFFICIAL: "cn",
    game_info.CHANNEL_GLOBAL: "global",
}

#: 本地客户端进程名（与 BGI 侧口径一致），用来判「游戏正在运行」
_LOCAL_PROCESS_NAMES: tuple[str, ...] = ("YuanShen.exe", "GenshinImpact.exe")

#: 自动流程的单轮时限（分钟）：调度任务不能因为一次更新无限挂着。
#: 已完成的部分与下载缓存都会保留，下次从断点继续。
_AUTO_TIME_LIMIT_MIN = 180


async def ensure_game_updated(
    script_config: BetterGIConfig,
    user_config: BetterGIUserConfig,
    *,
    on_log: LogHook | None = None,
    should_abort: AbortHook | None = None,
) -> bool:
    """按开关接管原神客户端更新，返回本次任务能否继续。

    Args:
        script_config: BetterGI 脚本配置，提供总开关与 BGI 根目录。
        user_config: 当前用户配置，游戏路径可能被用户级覆盖。
        on_log: 一行进度文案的回调；不传则只写 app.log。
        should_abort: 中止判定，传给编排层在文件与数据块边界收工。

    Returns:
        ``True`` 表示可以继续本次任务（含更新成功、无需更新、以及「判不出来所以
        不打扰」）；``False`` 表示确知需要更新却没做成，要用户先处理。
    """
    if not script_config.get("Game", "IfAutoUpdate"):
        # 开关没开是常态，什么都不写：否则每次无关任务都要刷一行
        return True

    root_path = Path(str(script_config.get("Info", "RootPath") or "."))
    user_game_path = str(user_config.get("Switch", "GamePath") or "")
    try:
        game_exe = game_info.resolve_game_exe(root_path, user_game_path)
    except Exception as error:  # noqa: BLE001
        logger.warning("原神更新：解析游戏路径失败，本轮跳过 - {}", error)
        return True

    channel = game_info.detect_channel(game_exe)
    if channel == game_info.CHANNEL_BILIBILI:
        # B 服是独立渠道、版本节奏与官服不同，用官服清单更新会写坏客户端
        await _report(
            on_log, f"原神更新：{channel}客户端不支持自动更新，请用官方启动器更新"
        )
        return True

    resource = {"cn": "官服", "global": "国际服"}.get(
        _CHANNEL_TO_REGION.get(channel or "", "")
    )
    if resource is None:
        logger.warning("原神更新：未能识别客户端渠道（{}），本轮跳过", game_exe)
        return True

    running = game_info.find_running_game_exe(_LOCAL_PROCESS_NAMES)
    if running is not None:
        await _report(
            on_log, f"原神更新：检测到游戏正在运行（{running}），跳过更新以免写坏文件"
        )
        return True

    async def report(line: str) -> None:
        """只进调度台：更新过程的日志由编排层统一写 app.log，这里不再复述。"""
        await _report(on_log, line)

    result = await update_genshin_client(
        str(game_exe.parent),
        resource=resource,
        time_limit_min=_AUTO_TIME_LIMIT_MIN,
        on_progress=report,
        should_abort=should_abort,
    )

    if result.success:
        if not result.noop:
            # 真的更新了才值得占用任务日志；「已是最新」只留在 app.log
            await report(
                f"原神客户端更新完成 {result.local_version or '?'} -> "
                f"{result.remote_version or '?'}"
            )
        return True

    await report(f"原神客户端更新未完成：{result.message}")
    return False


async def _report(on_log: LogHook | None, line: str) -> None:
    """记一行日志并按需推给调度台。

    Args:
        on_log: 进度回调；``None`` 时只记日志。
        line: 面向用户的文案。
    """
    logger.info(line)
    if on_log is not None:
        await on_log(line)
