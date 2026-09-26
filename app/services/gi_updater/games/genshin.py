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

"""原神（Genshin Impact）的版本管理与安装器——属于这款游戏的钩子都在这。

原神的差异集中在几处：

* **双可执行名**：国际服 ``GenshinImpact.exe``、国服/B 服 ``YuanShen.exe``。每个探测
  都先试主名、再试备选名，并且要求二者**互斥**：``is_exec_data_dir_valid`` 只判定不改
  盘，混装时由宿主门禁停手并提示改用官方启动器。
* **语音清单**：``<GenshinImpact_Data|YuanShen_Data>/Persistent/audio_lang_14``，
  里面写的是语言全名 ``Chinese`` / ``English(US)`` / ``Japanese`` / ``Korean``，
  读回时要映射成 locale code。
* **3.6 语音目录迁移**：安装前把
  ``<Data>/StreamingAssets/Audio/GeneratedSoundBanks/Windows``
  搬到 ``<Data>/StreamingAssets/AudioAssets``。
* **强制 Sophon**：自 5.6 起官方不再提供 zip 包，预设里
  ``is_force_redirect_to_sophon = True``，因此上游的 ``@DisableSophon`` 与启动器开关
  对原神**无效**；``pkg_version`` 只能从 Sophon 清单伪造（见 :mod:`api`）。
* **无 DeltaPatch**。

一款游戏的知识集中在本模块；加新游戏请另建一个同形态的模块，不改引擎正文。
"""

from __future__ import annotations

import os
import shutil
from typing import List, Optional

from app.services.gi_updater.games.spec import GameSpec, register
from app.services.gi_updater.install import InstallManagerBase, UpdatePlan
from app.services.gi_updater.presets import GameKey
from app.services.gi_updater.versioning import GameVersionBase

__all__ = [
    "GameTypeGenshinVersion",
    "GLOBAL_EXEC_NAME",
    "ALTERNATIVE_EXEC_NAME",
    "GenshinInstaller",
    "LANGUAGE_STRING_TO_LOCALE",
    "GENSHIN",
]


GLOBAL_EXEC_NAME = "GenshinImpact.exe"
ALTERNATIVE_EXEC_NAME = "YuanShen.exe"

#: 原神语音清单里写的语言全名表


class GameTypeGenshinVersion(GameVersionBase):
    """原神版本管理。"""

    # ------------------------------------------------------------ 可执行名

    @property
    def alternative_executable_name(self) -> str:
        """与 `preset.executable_name` 互斥的**另一**客户端可执行名。

        原神双可执行名：国际服 ``GenshinImpact.exe``、国服/B 服 ``YuanShen.exe``。
        若当前是 ``YuanShen.exe`` 则备选用 ``GenshinImpact.exe``，反之亦然。

        Returns:
            备选可执行文件名（不含路径）。
        """
        return (
            GLOBAL_EXEC_NAME
            if self.preset.executable_name == ALTERNATIVE_EXEC_NAME
            else ALTERNATIVE_EXEC_NAME
        )

    def _candidate_executable_names(self) -> List[str]:
        """覆写 :meth:`GameVersionBase._candidate_executable_names`。

        原神有两个**互斥**客户端：国际服 ``GenshinImpact.exe`` 与国服/B 服
        ``YuanShen.exe``。每个探测都「主名 -> 备选名」试两遍；这里返回
        ``[主名, 备选名]``，使 `is_game_installed` 命中任一即算已安装。
        """
        return [self.preset.executable_name, self.alternative_executable_name]

    def is_exec_data_dir_valid(self) -> bool:
        """判断当前目录是否「未混装」两个原神客户端。

        原神国际服（``GenshinImpact.exe``）与国服/B 服（``YuanShen.exe``）的数据目录
        互斥，不能放在同一目录。判定：若某客户端的**另一个**客户端可执行文件或其
        ``<名>_Data`` 目录已存在，且**当前**客户端自身也存在，则视为「混装」返回 ``False``。

        Returns:
            未混装（或 `game_path` 为空，视为无需校验）为 ``True``。

        Note:
            混装时由官方启动器纠偏；本包只判定并拒绝放行。
        """
        if not self.game_path:
            return True

        primary = self.preset.executable_name
        alternative = self.alternative_executable_name
        for name in (primary, alternative):
            other = alternative if name == primary else primary
            other_exec = os.path.join(self.game_path, other)
            other_dir = os.path.join(self.game_path, os.path.splitext(other)[0])
            if os.path.isfile(other_exec) or os.path.isdir(other_dir):
                # 只有在当前客户端自身存在时才判定为「混装」
                if os.path.isfile(os.path.join(self.game_path, name)):
                    return False
        return True

    # ------------------------------------------------------------ 语音

    def audio_lang_list_path(self) -> Optional[str]:
        """覆写 :meth:`GameVersionBase.audio_lang_list_path`。

        原神不固定清单文件名：直接扫描 ``<GameName>_Data/Persistent`` 下首个
        以 ``audio_lang_`` 开头的文件（兼容 ``audio_lang_14`` 等版本相关命名）。

        Returns:
            找到则返回该文件路径，目录不存在或无匹配文件时返回 ``None``。
        """
        persistent = self.game_data_persistent_path
        if not os.path.isdir(persistent):
            return None
        for name in os.listdir(persistent):
            if name.startswith("audio_lang_"):
                return os.path.join(persistent, name)
        return None

    def audio_lang_list_path_static(self) -> str:
        """覆写 :meth:`GameVersionBase.audio_lang_list_path_static`，固定指向 ``audio_lang_14``。"""
        return os.path.join(self.game_data_persistent_path, "audio_lang_14")

    # ------------------------------------------------------------ 3.6 迁移

    @property
    def audio_old_path(self) -> str:
        """3.6 迁移前的旧语音目录 ``<Data>/StreamingAssets/Audio/GeneratedSoundBanks/Windows``。"""
        return os.path.join(
            self.game_data_path,
            "StreamingAssets",
            "Audio",
            "GeneratedSoundBanks",
            "Windows",
        )

    @property
    def audio_new_path(self) -> str:
        """3.6 迁移后的新语音目录 ``<Data>/StreamingAssets/AudioAssets``。"""
        return os.path.join(self.game_data_path, "StreamingAssets", "AudioAssets")


#: 语言全名 -> locale code；原神语音清单里存的是全名
LANGUAGE_STRING_TO_LOCALE = {
    "Chinese": "zh-cn",
    "Chinese(PRC)": "zh-cn",
    "English": "en-us",
    "English(US)": "en-us",
    "Korean": "ko-kr",
    "Japanese": "ja-jp",
}

LOCALE_TO_LANGUAGE_STRING = {
    "zh-cn": "Chinese",
    "en-us": "English(US)",
    "ja-jp": "Japanese",
    "ko-kr": "Korean",
}


class GenshinInstaller(InstallManagerBase):
    """原神安装器。"""

    #: 类型标注，方便 IDE
    version: GameTypeGenshinVersion

    # ------------------------------------------------------------ 语音

    def locale_code_from_language_string(self, text: str) -> str:
        """把语言全名映射成 locale code（覆写 :meth:`InstallManagerBase.locale_code_from_language_string`）。

        Args:
            text: 清单里的一行（如 ``Chinese`` / ``English(US)``）。

        Returns:
            对应的 locale code（如 ``zh-cn`` / ``en-us``）；未知全名退化为
            ``text.lower()``。

        Note:
            原神清单里存的是 ``Chinese`` 这类全名，而基类默认直接 ``lower()``；
            本覆写通过 ``LANGUAGE_STRING_TO_LOCALE`` 做显式映射。
        """
        key = text.strip()
        return LANGUAGE_STRING_TO_LOCALE.get(key, key.lower())

    def language_string_from_locale_code(self, locale_code: str) -> str:
        """把 locale code 映射回语言全名（``locale_code_from_language_string`` 的逆函数）。

        Args:
            locale_code: locale code（如 ``zh-cn`` / ``en-us``）。

        Returns:
            语言全名（如 ``Chinese`` / ``English(US)``）；未知 code 原样返回。
        """
        return LOCALE_TO_LANGUAGE_STRING.get(locale_code.lower(), locale_code)

    def write_audio_lang_list(self, languages) -> None:
        """覆写：写回 ``audio_lang_14``（内容是语言全名而非 locale code）。

        Args:
            languages: 要写入的语音 locale code 列表。

        Note:
            与基类不同，原神把每个 locale code 经 :meth:`language_string_from_locale_code`
            转成 ``Chinese`` / ``English(US)`` 等全名再写盘；拿不到路径时直接跳过（不写盘）。
        """
        path = self.version.audio_lang_list_path_static()
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for language in languages:
                handle.write(f"{self.language_string_from_locale_code(language)}\n")

    # ------------------------------------------------------------ 迁移

    def before_install(self, plan: UpdatePlan) -> None:
        """覆写：安装前做 3.6 语音目录迁移的前置检查。

        Args:
            plan: 当前执行计划（本覆写未使用，仅保持接口一致）。

        Note:
            迁移的具体动作与搬移计数由 :meth:`migrate_audio_directory` 负责。
        """
        self.migrate_audio_directory()

    def migrate_audio_directory(self) -> int:
        """把 3.6 之前的语音目录搬到新目录，返回搬移的文件数。

        Returns:
            实际搬移的文件数；旧目录不存在时返回 0。
        """
        version = self.version
        old_path = version.audio_old_path
        new_path = version.audio_new_path
        if not os.path.isdir(old_path):
            return 0

        moved = 0
        offset = len(old_path) + 1
        for root, _dirs, files in os.walk(old_path):
            for name in files:
                source = os.path.join(root, name)
                relative = source[offset:]
                target = os.path.join(new_path, relative)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                if os.path.isfile(target):
                    os.remove(target)
                shutil.move(source, target)
                moved += 1

        self.logger.info("已迁移 %d 个语音文件：%s -> %s", moved, old_path, new_path)
        return moved

    # ------------------------------------------------------------ 可执行名纠偏

    def validate_exec_data_dir(self) -> bool:
        """判断可执行目录是否有效（防国际服/国服客户端混装）。

        Returns:
            委托 ``version.is_exec_data_dir_valid()`` 判定（混装时为 False）。
        """
        return self.version.is_exec_data_dir_valid()


# ------------------------------------------------------------ 注册

GENSHIN = register(
    GameSpec(
        key=GameKey.Genshin,
        display_name="原神",
        version_cls=GameTypeGenshinVersion,
        installer_cls=GenshinInstaller,
    )
)
