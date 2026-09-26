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

"""版本号值类型与安装状态机。

米哈游使用 4 段版本号（major.minor.patch.revision，例如 3.4.0.0 / 2.7.10.1）。
比较语义：允许短版本（``"3.4" == "3.4.0.0"``）、逐段数值比较而非字符串比较、
未解析或空串视为 ``None``（表示「未安装」或「未知」）。

状态机（对应上游的 ``.GameApi`` / ``.GameState`` / ``.IniConfig`` 三个分部）：

  * 只读 / 写**本地版本状态**（``config.ini``）
  * 从 API 结果里算**远程版本**（Sophon tag 优先于 zip major.version）
  * 用 ``get_state()`` 给出安装态之一（见 :class:`GameInstallStateEnum`：5 个取值）
  * 按状态给出「本次要下的包清单」（差分优先，退回全量）

它不碰网络、不碰磁盘大文件——那是 :mod:`sophon` / :mod:`install` 的事。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Optional, Tuple

from app.services.gi_updater.api import LauncherApi
from app.services.gi_updater.common import (
    PROFILE_SECTION,
    VERSION_SECTION,
    IniFile,
    get_logger,
)
from app.services.gi_updater.presets import PresetConfig

__all__ = [
    "GameVersion",
    "VERSION_SEGMENTS",
    "GameInstallStateEnum",
    "GameVersionBase",
    "GAME_STATE_LABELS",
]


#: 版本号段数，米哈游固定为 4 段
VERSION_SEGMENTS = 4

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass(frozen=True, order=False)
class GameVersion:
    """不可变、可比较的 4 段版本号。

    ：使用 ``field`` 关键字做惰性解析，并重载了
    ``==`` / ``>`` / ``<`` 等运算符。Python 侧用 ``functools.total_ordering``
    风格的显式富比较方法实现同样语义。
    """

    major: int = 0
    minor: int = 0
    patch: int = 0
    revision: int = 0

    # ------------------------------------------------------------------ 构造

    @classmethod
    def parse(cls, value: "str | int | GameVersion | None") -> Optional["GameVersion"]:
        """宽松解析版本号；无法解析或为空时返回 ``None`` 而非抛异常。

         / 隐式转换运算符。

        Args:
            value: 待解析的值，支持 str（如 ``"5.6.0.0"``）、int、``GameVersion``
                或 None。空串、``None``、以及无法匹配 ``_VERSION_RE`` 的输入一律返回 ``None``。

        Returns:
            解析出的 :class:`GameVersion`，或 ``None``（语义上表示「未安装」/「未知」）。
        """
        if value is None:
            return None
        if isinstance(value, GameVersion):
            return value
        if isinstance(value, int):
            # 极少见：某些 API 用纯数字表示版本
            return cls.from_segments([value])

        text = str(value).strip()
        if not text:
            return None

        match = _VERSION_RE.match(text)
        if match is None:
            return None

        segments = [int(part) for part in match.groups() if part is not None]
        return cls.from_segments(segments)

    @classmethod
    def from_segments(cls, segments: Iterable[int]) -> "GameVersion":
        """用整数序列构造版本号，超过 4 段截断、不足 4 段补 0。

        Args:
            segments: 版本号分段，如 ``[5, 6, 0]`` 或 ``(3, 4)``。

        Returns:
            补齐/截断到 4 段后的 :class:`GameVersion` 实例。
        """
        parts: List[int] = [int(part) for part in segments][:VERSION_SEGMENTS]
        while len(parts) < VERSION_SEGMENTS:
            parts.append(0)
        return cls(*parts)

    @classmethod
    def empty(cls) -> "GameVersion":
        """等价（全 0 版本）。"""
        return cls(0, 0, 0, 0)

    # ------------------------------------------------------------------ 输出

    @property
    def version_string(self) -> str:
        """（``major.minor.patch.revision``）。"""
        return f"{self.major}.{self.minor}.{self.patch}.{self.revision}"

    @property
    def sophon_tag(self) -> str:
        """Sophon 侧的 3 段版本串（``major.minor.patch``）。

        Note:
            分支 ``tag`` 与差分清单 ``stats`` 的基线键都是 3 段形态，拿 4 段的
            `version_string` 去对永远对不上：目标版本会被服务端判 ``-202``，
            差分会被当成不存在。
        """
        return f"{self.major}.{self.minor}.{self.patch}"

    def as_tuple(self) -> Tuple[int, int, int, int]:
        """返回 4 段版本号的整数元组 ``(major, minor, patch, revision)``。"""
        return (self.major, self.minor, self.patch, self.revision)

    def __str__(self) -> str:  # pragma: no cover - 平凡实现
        """等价于 ``version_string``。"""
        return self.version_string

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        """返回可供 ``eval``/调试阅读的 ``GameVersion(x.y.z.w)`` 形式。"""
        return f"GameVersion({self.version_string})"

    def is_empty(self) -> bool:
        """判断是否为全 0 的「空版本」。"""
        return self.as_tuple() == (0, 0, 0, 0)

    # ------------------------------------------------------------------ 比较

    def __eq__(self, other: object) -> bool:
        """相等比较；右操作数经 :func:`GameVersion.parse` 转换后逐段比较。

        Args:
            other: 另一版本，可为 ``GameVersion`` / str / int / None。

        Returns:
            两段版本是否相等；``other`` 为 None 或无法解析时返回 ``NotImplemented``，
            交由 Python 回退到默认对象比较（而非当作不等）。
        """
        other_version = (
            self.parse(other) if not isinstance(other, GameVersion) else other
        )
        if other_version is None:
            return NotImplemented
        return self.as_tuple() == other_version.as_tuple()

    def __hash__(self) -> int:
        """以 4 段元组为哈希键，使相等的版本必然同哈希。"""
        return hash(self.as_tuple())

    def __lt__(self, other: "GameVersion") -> bool:
        """小于比较；右操作数经 :func:`GameVersion.parse` 转换后逐段比较。

        Args:
            other: 另一版本，可为 ``GameVersion`` / str / int / None。
                None 或无法解析的值按 ``GameVersion.Empty`` 处理，故「未安装」小于任何具体版本。

        Returns:
            左操作数是否严格小于 ``other``。
        """
        return self.as_tuple() < _coerce(other).as_tuple()

    def __le__(self, other: "GameVersion") -> bool:
        """小于等于比较；右操作数经 :func:`GameVersion.parse` 转换后逐段比较。

        Args:
            other: 另一版本，可为 ``GameVersion`` / str / int / None。
                None 或无法解析的值按 ``GameVersion.Empty`` 处理。

        Returns:
            左操作数是否小于或等于 ``other``。
        """
        return self.as_tuple() <= _coerce(other).as_tuple()

    def __gt__(self, other: "GameVersion") -> bool:
        """大于比较；右操作数经 :func:`GameVersion.parse` 转换后逐段比较。

        Args:
            other: 另一版本，可为 ``GameVersion`` / str / int / None。
                None 或无法解析的值按 ``GameVersion.Empty`` 处理。

        Returns:
            左操作数是否严格大于 ``other``。
        """
        return self.as_tuple() > _coerce(other).as_tuple()

    def __ge__(self, other: "GameVersion") -> bool:
        """大于等于比较；右操作数经 :func:`GameVersion.parse` 转换后逐段比较。

        Args:
            other: 另一版本，可为 ``GameVersion`` / str / int / None。
                None 或无法解析的值按 ``GameVersion.Empty`` 处理。

        Returns:
            左操作数是否大于或等于 ``other``。
        """
        return self.as_tuple() >= _coerce(other).as_tuple()


def _coerce(other: object) -> GameVersion:
    """把比较运算的右操作数转成 GameVersion。

    None 或无法解析的值按 ``GameVersion.Empty`` 处理，使「未安装」排在任意
    具体版本之下（「未知」排在「已安装」之前）。

    Args:
        other: 比较右操作数，可为 ``GameVersion`` / str / int / None。

    Returns:
        与 ``other`` 等价的 :class:`GameVersion`（不可解析时退化为全 0 空版本）。
    """
    if isinstance(other, GameVersion):
        return other
    parsed = GameVersion.parse(other)
    if parsed is None:
        # 与 None 比较时，任何具体版本都视为「更大」（已安装 > 未安装）
        return GameVersion.empty()
    return parsed


#: 判定「游戏已安装」时要求可执行文件的最小体积（1 << 16 = 64 KiB）
MIN_EXECUTABLE_SIZE = 1 << 16

GAME_STATE_LABELS = {
    "NotInstalled": "未安装",
    "GameBroken": "安装已损坏",
    "NeedsUpdate": "需要更新",
    "InstalledHavePreload": "已安装（有预下载）",
    "Installed": "已安装且为最新",
}


class GameInstallStateEnum(str, Enum):
    """安装状态机的五个取值。"""

    NotInstalled = "NotInstalled"
    GameBroken = "GameBroken"
    NeedsUpdate = "NeedsUpdate"
    InstalledHavePreload = "InstalledHavePreload"
    Installed = "Installed"

    def __str__(self) -> str:  # pragma: no cover
        """返回本地化中文标签（来自 `GAME_STATE_LABELS`），未登记时退回原始枚举值。

        Note:
            标记 ``# pragma: no cover``：只是枚举值的展示别名，不参与任何逻辑分支。
        """
        return GAME_STATE_LABELS.get(self.value, self.value)


class GameVersionBase:
    """版本管理基类；三款游戏通过子类覆写少量钩子。"""

    def __init__(
        self,
        preset: PresetConfig,
        launcher_api: Optional[LauncherApi] = None,
        game_path: Optional[str] = None,
    ) -> None:
        """初始化版本管理实例并惰性加载本地 ``config.ini``。

        Args:
            preset: 启动器 profile 配置（`PresetConfig`），提供可执行名 / 频道 / 区域等。
            launcher_api: 已初始化的 `LauncherApi`（含远程版本与 Sophon 分支）；
                传 ``None`` 时本实例只能读本地状态、算不出远程版本。
            game_path: 游戏安装根目录；非空时立即调用 `update_game_path` 载入 ini
                （``save=False``，构造阶段不写盘）。

        Note:
            仅建立内存态与 ini 句柄；真正写盘发生在 `update_game_path` /
            `update_game_version` 等显式调用时。
        """
        self.preset = preset
        self.launcher_api = launcher_api
        self.logger = get_logger()

        #: 游戏安装目录
        self.game_path: str = ""
        #: 启动器 profile 目录（存放 [launcher] config.ini）
        self.profile_dir: Optional[str] = None

        self.game_ini_version = IniFile()
        self.game_ini_profile = IniFile()

        if game_path:
            self.update_game_path(game_path, save=False)

    # ================================================================== 路径

    @property
    def config_file_name(self) -> str:
        """磁盘配置文件名，固定为 ``config.ini``（本地版本与 profile 共用此名）。

        Returns:
            文件名；游戏根目录与 profile 目录各持有一份同名文件。
        """
        return "config.ini"

    @property
    def game_ini_version_path(self) -> str:
        """``<game_path>/config.ini`` —— 存 ``game_version`` 的那份。"""
        return os.path.join(self.game_path or "", self.config_file_name)

    @property
    def game_ini_profile_path(self) -> str:
        """``<profile_dir>/config.ini`` —— 存 ``game_install_path`` 的那份。"""
        return os.path.join(self.profile_dir or "", self.config_file_name)

    @property
    def game_data_path(self) -> str:
        """``<game_path>/<ExecName>_Data``。"""
        exec_prefix = os.path.splitext(self.preset.executable_name)[0]
        return os.path.join(self.game_path, f"{exec_prefix}_Data")

    @property
    def game_data_persistent_path(self) -> str:
        """``<Data>/Persistent`` 目录。"""
        return os.path.join(self.game_data_path, "Persistent")

    # ================================================================== ini

    def update_game_path(self, path: str, save: bool = True) -> None:
        """重新指向游戏目录并（重新）加载本地 ``config.ini``。

        Args:
            path: 新的游戏根目录；空串会清空 `game_path`（用于取消绑定）。
            save: 为 ``True`` 且 `profile_dir` 存在时，把 ``game_install_path`` 写回
                ``<profile_dir>/config.ini``；构造期调用传 ``False`` 避免误写。

        Note:
            仅加载、不校验目录是否合法；会重建 `game_ini_version` / `game_ini_profile`
            两个句柄（丢弃此前内存中的未保存修改）。
        """
        self.game_path = os.path.abspath(path) if path else ""
        self.game_ini_version = IniFile()
        self.game_ini_profile = IniFile()

        if self.profile_dir:
            self.game_ini_profile = IniFile.load(self.game_ini_profile_path)
        if self.game_path and os.path.isfile(self.game_ini_version_path):
            self.game_ini_version = IniFile.load(self.game_ini_version_path)

        if save and self.profile_dir:
            self.game_ini_profile[PROFILE_SECTION]["game_install_path"] = (
                self.game_path.replace("\\", "/")
            )
            self.game_ini_profile.save(self.game_ini_profile_path)

    def reload(self) -> None:
        """按当前 ``game_path`` 重新加载本地 ini。"""
        self.update_game_path(self.game_path, save=False)

    @property
    def version_section(self):
        """`game_ini_version` 中存放版本信息的节（``[General]`` 即 `VERSION_SECTION`）。

        Returns:
            mapping: ini 节视图；读写它等价于直接读写本地 ``config.ini`` 的版本字段。
        """
        return self.game_ini_version[VERSION_SECTION]

    # ================================================================== 版本

    @property
    def installed_version(self) -> Optional[GameVersion]:
        """从 ``config.ini [General] game_version`` 读。"""
        raw = self.version_section.get("game_version")
        return GameVersion.parse(raw)

    def update_game_version(
        self, version: Optional[GameVersion], save: bool = True
    ) -> None:
        """把本地 ``game_version`` 写入 ``config.ini``。

        Args:
            version: 目标版本；``None`` 时写入空串（等同于「未知 / 未安装」）。
            save: 为 ``True`` 立即落盘，否则只改内存。

        Note:
            仅改 ``[General] game_version`` 一个字段；写盘委托 `save_version_config`。
        """
        self.version_section["game_version"] = version.version_string if version else ""
        if save:
            self.save_version_config()

    def update_game_version_to_latest(self, save: bool = True) -> None:
        """把本地版本同步为远程最新，并回写频道信息。

        Args:
            save: 传给 `update_game_version` / `update_game_channels` 的落盘开关。

        Note:
            顺序固定为先写版本、后写 channel / sub_channel / cps；
            跳过写盘时不会引发后续逻辑自动补写。
        """
        self.update_game_version(self.latest_version, save=save)
        self.update_game_channels(save=save)

    def update_game_channels(self, save: bool = True) -> None:
        """把当前 profile 的 channel / sub_channel / cps 写回 ``config.ini``。

        Args:
            save: 为 ``True`` 立即落盘，否则只改内存中的 `version_section`。

        Note:
            三个值来自 `preset`，与游戏实际区域绑定；改完需落盘才对后续启动生效。
        """
        self.version_section["channel"] = str(self.preset.channel_id)
        self.version_section["sub_channel"] = str(self.preset.sub_channel_id)
        self.version_section["cps"] = self.preset.cps
        if save:
            self.save_version_config()

    def save_version_config(self) -> None:
        """把内存中的 `game_ini_version` 落盘到 ``<game_path>/config.ini``。

        Note:
            `game_path` 为空时直接返回、不写盘。`update_game_version` /
            `update_game_channels` 默认都会调用本方法（受各自 ``save`` 形参控制）。
        """
        if not self.game_path:
            return
        self.game_ini_version.save(self.game_ini_version_path)

    # ------------------------------------------------------------ 远程版本

    @property
    def sophon_branch(self):
        """主版本 Sophon 分支（`launcher_api.sophon_branches.main`）。

        Returns:
            Optional[...]: 分支对象；`launcher_api` 未初始化或尚无 Sophon 数据时返回 ``None``。

        Note:
            其 ``tag`` 是 API 返回的原始 3 段版本串（如 ``7.0.0``），`latest_version`
            直接解析它，切勿补成 4 段（服务端会以 ``-202 not found`` 拒绝）。
        """
        if self.launcher_api is None or self.launcher_api.sophon_branches is None:
            return None
        return self.launcher_api.sophon_branches.main

    @property
    def sophon_preload_branch(self):
        """预下载 Sophon 分支（`launcher_api.sophon_branches.pre_download`）。

        Returns:
            Optional[...]: 预下载分支；无预下载或 API 未初始化时返回 ``None``。

        Note:
            与 `sophon_branch` 同源，仅在游戏开放预下载期间非空；
            其 ``tag`` 同样是原始 3 段版本串。
        """
        if self.launcher_api is None or self.launcher_api.sophon_branches is None:
            return None
        return self.launcher_api.sophon_branches.pre_download

    @property
    def zip_package(self):
        """主版本 zip 资源包（`resource_package.main_package`）。

        Returns:
            Optional[HypPackageData]: 全量 / 差分 zip 包元数据；API 未初始化或无数据时
                返回 ``None``（此时 `latest_version` 只能依赖 Sophon）。

        Note:
            自原神 5.6 起官方不再提供 zip 包，该字段可能为 ``None``，
            因此远程版本判定以 Sophon 为主、zip 为辅。
        """
        if self.launcher_api is None or self.launcher_api.resource_package is None:
            return None
        return self.launcher_api.resource_package.main_package

    @property
    def zip_preload_package(self):
        """预下载 zip 资源包（`resource_package.pre_download`）。

        Returns:
            Optional[HypPackageData]: 预下载 zip 包；无预下载或 API 未初始化时返回 ``None``。
        """
        if self.launcher_api is None or self.launcher_api.resource_package is None:
            return None
        return self.launcher_api.resource_package.pre_download

    @property
    def latest_version(self) -> Optional[GameVersion]:
        """远程最新版本。

        综合 Sophon 分支 ``tag`` 与 zip 包的 ``current_version`` 取较大者：
        若 Sophon 版本更高则返回 Sophon，否则返回两者中非空的那一个
        （Sophon 与 zip 相等时偏 Sophon）。

        Returns:
            解析出的版本；Sophon 与 zip 双双缺失时返回 ``None``
                （表示拿不到远程版本）。

        Note:
            自原神 5.6 起官方只发 Sophon、不再提供 zip 包，zip 的 major 可能滞后甚至
            被伪造，因此以 Sophon 为主、zip 为辅。Sophon ``tag`` 必须是 API 返回的
            **原始 3 段版本串**（如 ``7.0.0``）；补成 4 段（``7.0.0.0``）会被服务端以
            ``-202 not found`` 拒绝。
        """
        version_from_sophon = (
            GameVersion.parse(self.sophon_branch.tag) if self.sophon_branch else None
        )
        version_from_zip = (
            GameVersion.parse(self.zip_package.current_version.version)
            if self.zip_package and self.zip_package.current_version
            else None
        )

        if (
            version_from_sophon is not None
            and version_from_zip is not None
            and version_from_sophon > version_from_zip
        ):
            return version_from_sophon
        return version_from_sophon or version_from_zip

    @property
    def preload_version(self) -> Optional[GameVersion]:
        """远程预下载版本。

        与 `latest_version` 同源，只是换成预下载分支 / 预下载 zip 包；
        取 Sophon 与 zip 的较大者。

        Returns:
            有预下载时返回版本，否则 ``None``。

        Note:
            仅当官方开放预下载期间非空（见 `is_game_has_preload`）；
            Sophon ``tag`` 同样是原始 3 段版本串。
        """
        version_from_sophon = (
            GameVersion.parse(self.sophon_preload_branch.tag)
            if self.sophon_preload_branch
            else None
        )
        version_from_zip = (
            GameVersion.parse(self.zip_preload_package.current_version.version)
            if self.zip_preload_package and self.zip_preload_package.current_version
            else None
        )
        if (
            version_from_sophon is not None
            and version_from_zip is not None
            and version_from_sophon > version_from_zip
        ):
            return version_from_sophon
        return version_from_sophon or version_from_zip

    # ================================================================== 状态

    def is_game_installed(self) -> bool:
        """判断游戏是否已安装。

        三重门槛：
            1. `game_path` 非空；
            2. 能从 ``config.ini`` 解析出 `installed_version`（即已记录版本号）；
            3. 候选可执行名中存在一个文件，且体积 ``> MIN_EXECUTABLE_SIZE``（64 KiB）。

        Returns:
            三者同时满足为 ``True``。

        Note:
            可执行文件体积门槛 ``MIN_EXECUTABLE_SIZE = 1 << 16``（64 KiB）
            用来排除残破 / 占位的可执行文件。

            本方法把「有可执行文件、但 ``config.ini`` 里没版本号」也判为未安装；
            :meth:`get_state` 对同一情形判为 ``GameBroken``。两者分工不同：本方法回答
            「能不能当成一个装好的游戏来读」，``get_state`` 回答「该给用户看哪个状态」。
        """
        return self.installed_version is not None and self._has_installed_executable()

    def _has_installed_executable(self) -> bool:
        """探测游戏根目录下是否存在「体积达标」的可执行文件（不看版本号）。

        Returns:
            `game_path` 非空，且 :meth:`_candidate_executable_names` 中任一名字
                对应一个体积 ``> MIN_EXECUTABLE_SIZE`` 的普通文件时为 ``True``。

        Note:
            ``game_path`` 的空判必须留在本方法内：一旦为空，``os.path.join("", name)``
            得到的是相对路径，会误命中当前工作目录下的同名文件。
        """
        if not self.game_path:
            return False
        for executable_name in self._candidate_executable_names():
            path = os.path.join(self.game_path, executable_name)
            if os.path.isfile(path) and os.path.getsize(path) > MIN_EXECUTABLE_SIZE:
                return True
        return False

    def _candidate_executable_names(self) -> List[str]:
        """列出用于「是否存在可执行文件」判定的候选名（模板钩子）。

        基类默认只返回 `preset.executable_name` 一个；子类覆写以容纳多客户端
        （如原神的国服 / 国际服互斥双名）。

        Returns:
            候选可执行文件名（不含路径）。

        Note:
            子类覆写时应保持语义：返回的每一个名字都代表「同一游戏的不同客户端」，
            `is_game_installed` 只要命中任一即视为已安装。
        """
        return [self.preset.executable_name]

    def is_game_version_match(self) -> bool:
        """本地版本与远程最新是否一致。"""
        return self.installed_version == self.latest_version

    def is_game_has_preload(self) -> bool:
        """是否存在可用的预下载版本。"""
        if self.is_use_sophon():
            return self.sophon_preload_branch is not None
        return bool(
            self.zip_preload_package and self.zip_preload_package.current_version
        )

    def is_use_sophon(self) -> bool:
        """版本侧「是否具备 / 应使用 Sophon」的能力判定。

        判定顺序：
            1. `preset.launcher_resource_chunks_url` 为 ``None``（无分块资源地址）
               -> 直接 ``False``（根本用不了 Sophon）；
            2. `preset.is_force_redirect_to_sophon` 为真 -> ``True``（强制重定向）；
            3. 否则看 `launcher_api.is_force_redirect_to_sophon`（服务端假版本触发）。

        Returns:
            上述逻辑结果。

        Note:
            实际的「用户是否关闭 / 是否存在 ``@DisableSophon`` 文件」等运行期开关
            在 install 层叠加，本方法只负责「能力与强制」这一半。
        """
        if self.preset.launcher_resource_chunks_url is None:
            return False
        if self.preset.is_force_redirect_to_sophon:
            return True
        return (
            self.launcher_api is not None
            and self.launcher_api.is_force_redirect_to_sophon
        )

    def is_force_redirect_to_sophon(self) -> bool:
        """是否「强制」走 Sophon（能力判断为真 **且** profile 显式开启强制）。

        Returns:
            ``is_use_sophon() and preset.is_force_redirect_to_sophon``。

        Note:
            其中的强制标志由 `LauncherApi._initialize_fake_version_info` 在
            服务端下发假版本信息时设置，用于把原本走 zip 的游戏也重定向到 Sophon。
        """
        return self.is_use_sophon() and self.preset.is_force_redirect_to_sophon

    def get_state(self) -> GameInstallStateEnum:
        """唯一的安装态判定入口。

        判定顺序（按实现）：
            1. :meth:`_has_installed_executable` 为 ``False``（没游戏目录 / 找不到
               体积达标的可执行文件）-> ``NotInstalled``；
            2. `is_game_installed()` 为 ``False``——此时可执行文件已在，差的只有
               ``config.ini`` 里的版本号 -> ``GameBroken``；
            3. 版本不一致（非最新）-> ``NeedsUpdate``；
            4. `is_game_has_preload()` 为真 -> ``InstalledHavePreload``；
            5. 其余 -> ``Installed``（已安装且最新）。

        Returns:
            上述 5 个状态之一。

        Note:
            ``GameBroken`` 的含义是「可执行文件在、``config.ini`` 没有版本号」。它与
            ``NotInstalled`` 在下游规划里被归为一档，区别只在给用户看的诊断标签：
            ``GAME_STATE_LABELS`` 会显示「安装已损坏」。
        """
        if not self._has_installed_executable():
            return GameInstallStateEnum.NotInstalled
        if not self.is_game_installed():
            # 有可执行文件但没有版本号 -> 视为损坏
            return GameInstallStateEnum.GameBroken
        if not self.is_game_version_match():
            return GameInstallStateEnum.NeedsUpdate
        if self.is_game_has_preload():
            return GameInstallStateEnum.InstalledHavePreload
        return GameInstallStateEnum.Installed

    # ================================================================== 本地探测
