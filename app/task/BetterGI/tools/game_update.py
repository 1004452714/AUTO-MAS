#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#
#   This file is part of AUTO-MAS.
#
#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.
#
#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

"""BetterGI 的原神客户端更新。

原神客户端由 BetterGI 自己启停，MAS 这边只经 ``game_info`` 拿到生效的游戏程序
路径（用户级 ``Switch.GamePath`` 优先，否则透传 BGI 全局配置），**不读 BGI 的其他
私有状态**。

本文件只做「读配置 → 判渠道 → 推调度台 → 决定是否继续任务」，查版本、下载、打补丁
与落盘都在 :mod:`app.services.genshin_updater`。两种入口共用同一条判定链：

- **自动入口**（:func:`ensure_game_updated` 默认）：任务启动游戏前按 ``Game.IfAutoUpdate``
  开关检查，查不到/环境不允许就放行，确知需要更新却做不了才阻断；
- **手动入口**（``manual=True``）：用户页「检查更新」用户主动触发，跳过开关，
  凡 MAS 无法自动完成的都直接抛错，由用户自己决定后续处理。

两种入口共同的行为策略：

- **只自动应用增量包**；拿不到差分（全新安装、逐文件全量比对）就**停手并明确指出**，
  建议用户用官方启动器——无人值守的任务不该顺手吃掉几十 GB；
- **查不到就放行**：接口不可用或读不出渠道属于「无法判定」，旧客户端通常仍能登录，
  不该因为查不到版本就拦住用户；
- **需要更新却更新失败则阻断**：拿旧客户端撞登录只会白跑一轮；
- **B 服硬拒绝**：B 服是独立渠道、版本节奏与官服不同，用官服清单更新会写坏客户端。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.models.config import BetterGIConfig, BetterGIUserConfig
from app.services.genshin_updater import (
    KIND_LABELS,
    UpdateKind,
    cleanup_temp_dir,
    execute_plan,
    plan_update,
)
from app.task.BetterGI.tools import game_info
from app.utils import get_logger
from app.utils.hpatchz import ensure_hpatchz

logger = get_logger("原神更新 BetterGI")

#: 一行面向用户的进度文案
LogHook = Callable[[str], Awaitable[None]]

#: 中止判定；为真时更新在文件批次边界收工
AbortHook = Callable[[], bool]


def task_stopped(task: object) -> bool:
    """用户是否已要求停止本任务（供更新途中在批次边界轮询）。

    与 HSR 的 ``_update_aborted`` 同口径：``stopped_manually`` 覆盖「停止落在
    任务主体里」，根任务已被取消则覆盖「停止落在更新途中」——两个都看，用户
    按下停止后才不必等一次几 GB 的下载走完。

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


#: 客户端渠道 → 更新接口区服；不在表里的渠道不接管
_CHANNEL_TO_REGION: dict[str, str] = {
    game_info.CHANNEL_OFFICIAL: "cn",
    game_info.CHANNEL_GLOBAL: "global",
}

#: 本地客户端进程名（与 BGI 侧口径一致）
_LOCAL_PROCESS_NAMES: tuple[str, ...] = ("YuanShen.exe", "GenshinImpact.exe")


async def _report(on_log: LogHook | None, line: str) -> None:
    """记一行日志并推给调度台。

    Args:
        on_log: 进度回调；``None`` 时只记日志。
        line: 面向用户的文案。
    """
    logger.info(line)
    if on_log is not None:
        await on_log(line)


async def ensure_game_updated(
    script_config: BetterGIConfig,
    user_config: BetterGIUserConfig,
    *,
    on_log: LogHook | None = None,
    manual: bool = False,
    should_abort: AbortHook | None = None,
) -> bool:
    """检查并按需做原神客户端增量更新。

    Args:
        script_config: BetterGI 脚本配置。
        user_config: 当前用户配置（游戏路径可能被用户级覆盖）。
        on_log: 一行进度文案的回调。
        manual: 是否为用户页「检查更新」手动触发。``True`` 时忽略
            ``Game.IfAutoUpdate`` 开关，且凡 MAS 无法自动完成的都抛
            ``RuntimeError``——用户主动发起就该得到明确的失败原因，而不是
            像自动流程那样静默放行。
        should_abort: 中止判定；传给执行层在文件批次边界收工。

    Returns:
        是否可以继续本次任务。``False`` 表示需要用户先处理——要么本次只能全量更新
        （已在调度台说明），要么更新确认需要却失败。``manual=True`` 时不会返回
        ``False``，这类情况一律抛错。

    Raises:
        RuntimeError: ``manual=True`` 且无法自动完成时（路径/渠道不可用、游戏运行中、
            无法判定、非增量、补丁工具缺失、执行失败）。
    """
    if not manual and not script_config.get("Game", "IfAutoUpdate"):
        # 开关没开是常态，什么都不写
        return True

    root_path = Path(str(script_config.get("Info", "RootPath") or "."))
    user_game_path = str(user_config.get("Switch", "GamePath") or "")
    try:
        game_exe = game_info.resolve_game_exe(root_path, user_game_path)
    except Exception as error:  # noqa: BLE001
        if manual:
            raise RuntimeError(f"无法确定游戏路径：{error}") from error
        logger.warning("原神更新：解析游戏路径失败，本轮跳过 - {}", error)
        return True

    channel = game_info.detect_channel(game_exe)
    if channel == game_info.CHANNEL_BILIBILI:
        if manual:
            raise RuntimeError(
                f"{channel}客户端（{game_exe}）不在自动更新支持范围，请用官方启动器更新"
            )
        await _report(
            on_log,
            f"原神更新：{channel}客户端（{game_exe}）不在自动更新支持范围，请用官方启动器更新",
        )
        return True

    region = _CHANNEL_TO_REGION.get(channel or "")
    if region is None:
        if manual:
            raise RuntimeError(
                f"未能识别客户端渠道（{game_exe}），请用官方启动器确认版本"
            )
        logger.warning("原神更新：未能识别客户端渠道（{}），本轮跳过", game_exe)
        return True

    running = game_info.find_running_game_exe(_LOCAL_PROCESS_NAMES)
    if running is not None:
        if manual:
            raise RuntimeError(f"检测到游戏正在运行（{running}），请先完全退出游戏")
        await _report(
            on_log, f"原神更新：检测到游戏正在运行（{running}），跳过更新以免写坏文件"
        )
        return True

    plan = await plan_update(game_exe.parent, region=region)

    if plan.kind is UpdateKind.NOOP:
        # 版本已追平，上次失败残留的续传流水账不再可信，顺手清掉
        await asyncio.to_thread(cleanup_temp_dir, game_exe.parent)
        if manual:
            await _report(
                on_log,
                f"当前为最新版本（{plan.local_version or '?'}），无需更新",
            )
        else:
            logger.info("原神客户端已是最新（{}）", plan.local_version)
        return True
    if plan.kind is UpdateKind.PRELOAD:
        await _report(on_log, f"原神更新：{plan.message}")
        return True
    if plan.kind is UpdateKind.UNKNOWN:
        if manual:
            raise RuntimeError(f"无法判定是否需要更新：{plan.message}")
        logger.warning("原神更新：无法判定是否需要更新，本轮跳过 - {}", plan.message)
        return True

    if plan.kind is not UpdateKind.PATCH:
        # 增量之外的种类必须明确指出，绝不静默执行全量
        if manual:
            raise RuntimeError(
                f"原神客户端需要{KIND_LABELS[plan.kind]}（{plan.local_version or '?'} -> "
                f"{plan.remote_version or '?'}）：MAS 只自动应用增量包，请用官方启动器更新"
            )
        await _report(
            on_log,
            f"原神客户端需要{KIND_LABELS[plan.kind]}（{plan.local_version or '?'} -> "
            f"{plan.remote_version or '?'}）：MAS 只自动应用增量包，"
            "本次已停止，请用官方启动器更新",
        )
        return False

    try:
        hpatchz = await ensure_hpatchz(on_progress=on_log)
    except Exception as error:  # noqa: BLE001
        if manual:
            raise RuntimeError(f"获取增量补丁工具失败（{error}）") from error
        await _report(on_log, f"原神更新：获取增量补丁工具失败（{error}），已停止")
        return False

    await _report(on_log, f"原神客户端{plan.describe()}")
    result = await execute_plan(
        plan, hpatchz=hpatchz, on_progress=on_log, should_abort=should_abort
    )

    if result.success:
        await _report(
            on_log,
            f"原神客户端更新完成 {plan.local_version or '?'} -> "
            f"{result.version or plan.remote_version or '?'}"
            + (f"，清理旧文件 {result.removed} 个" if result.removed else ""),
        )
        return True

    if manual:
        raise RuntimeError(f"更新未完成：{result.message}")
    await _report(on_log, f"原神客户端更新未完成：{result.message}")
    return False
