"""只读探测：拉真实清单算出计划，不下载、不写盘（诊断脚本，非 pytest 入口）。

用法：``python scripts/gi_update_check.py [--game gi]``，对所给游戏的两个区服各跑一次。
探测刻意走 ``dry_run`` 之外的真实路径：演练态会跳过清单收集，文件数与体积都会是
``getBuild`` 的统计估算而不是真值。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.gi_updater.common import summarize_size
from app.services.gi_updater.games import create_updater


async def probe(game: str, region: str, game_dir: str) -> dict:
    """按游戏与区服算一次更新计划并回报关键字段。

    Args:
        game: 游戏短名，如 ``gi``。
        region: ``cn`` / ``global``。
        game_dir: 一个空目录，代表「未安装」的全量场景。

    Returns:
        可 JSON 序列化的探测结果。
    """
    updater = create_updater(game, region, game_dir)
    plan = await asyncio.to_thread(updater.check)
    return {
        "game": game,
        "region": region,
        "profile": updater.preset.profile_name,
        "state": plan.state.value,
        "kind": plan.kind.value,
        "source": str(plan.source_version or ""),
        "target": str(plan.target_version or ""),
        "target_raw_tag": updater.installer._raw_target_tag(False),
        "matching_fields": list(plan.matching_fields),
        "file_count": plan.file_count,
        "total_size": plan.total_size,
        "total_size_text": summarize_size(plan.total_size),
        "assets_enum": len(plan.assets),
    }


def _game_arg() -> str:
    """取 ``--game`` 的值，缺省为原神。"""
    if "--game" in sys.argv:
        return sys.argv[sys.argv.index("--game") + 1]
    return "gi"


async def main() -> None:
    """对指定游戏的两个区服各跑一次只读探测并打印 JSON。"""
    game = _game_arg()
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for region in ("cn", "global"):
            try:
                results.append(await probe(game, region, tmp))
            except Exception as error:  # noqa: BLE001
                results.append(
                    {
                        "game": game,
                        "region": region,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
