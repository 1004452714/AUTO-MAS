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

"""原神客户端更新计划探测（**只读**）。

联网算出「本次要怎么更新」并打印结论，不写任何文件。用于在两处核对口径：

1. 开发期核对增量判定：能看到本次是增量还是非增量、差分清单点名的文件数、
   待下载字节、待删文件数，以及 Patch / CopyOver 各多少；
2. 实机核对体量：把待下载字节与官方启动器显示的更新体积对齐。

用法::

    python scripts/gi_update_check.py --dir "D:/Genshin Impact" --region cn
    python scripts/gi_update_check.py --dir "<国际服目录>" --region global
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.genshin_updater import (  # noqa: E402
    KIND_LABELS,
    get_region_preset,
    plan_update,
    summarize_size,
)


async def _run(game_dir: Path, region: str) -> int:
    """算出计划并打印。

    Args:
        game_dir: 游戏安装目录。
        region: 区服短名。

    Returns:
        进程退出码；协议失败为 1。
    """
    preset = get_region_preset(region)
    print(f"区服        : {preset.label}（{preset.key}）")
    print(f"游戏目录    : {game_dir}")
    print(f"目录存在    : {game_dir.is_dir()}")
    print("-" * 72)

    plan = await plan_update(game_dir, region=region)

    print(f"安装态      : {plan.state.value}")
    print(f"本地版本    : {plan.local_version or '（读不出）'}")
    print(f"远端版本    : {plan.remote_version or '（读不出）'}")
    print(f"计划种类    : {KIND_LABELS[plan.kind]}（{plan.kind.value}）")
    print(f"是否增量    : {'是' if plan.is_incremental else '否'}")
    print(f"需要动作    : {'是' if plan.needs_action else '否'}")
    print(f"概述        : {plan.describe()}")
    if plan.message:
        print(f"说明        : {plan.message}")

    summary = plan.patch
    if summary is not None:
        print("-" * 72)
        print(f"待更新文件  : {summary.file_count}")
        print(
            f"待下载字节  : {summary.download_size}（{summarize_size(summary.download_size)}）"
        )
        print(f"  ├ Patch   : {summary.patch_count} 个（用 hpatchz 打到旧文件）")
        print(f"  └ CopyOver: {summary.copyover_count} 个（分片本身即新内容）")
        print(f"待删旧文件  : {len(summary.removals)}")

    return 0 if plan.kind.value != "unknown" else 1


def main() -> int:
    """命令行入口。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(
        description="原神客户端更新计划探测（只读，不写任何文件）"
    )
    parser.add_argument("--dir", required=True, help="游戏安装目录（含 config.ini）")
    parser.add_argument(
        "--region",
        default="cn",
        choices=("cn", "global"),
        help="区服：cn=官服，global=国际服",
    )
    args = parser.parse_args()
    return asyncio.run(_run(Path(args.dir), args.region))


if __name__ == "__main__":
    raise SystemExit(main())
