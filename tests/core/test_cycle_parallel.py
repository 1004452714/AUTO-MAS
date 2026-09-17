#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team

#   This file is part of AUTO-MAS.

#   AUTO-MAS is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of
#   the License, or (at your option) any later version.

#   AUTO-MAS is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#   GNU Affero General Public License for more details.

#   You should have received a copy of the GNU Affero General Public License
#   along with AUTO-MAS. If not, see <https://www.gnu.org/licenses/>.

#   Contact: DLmaster_361@163.com

"""循环队列支持并行组（行内多脚本同步触发）相关的纯函数测试。

本次改造要点：
1. ``collect_cycle_entries`` 在产出 entry 时按邻接 Info.Parallel 字段把同一行
   的成员合并到行首 entry 的 ``sibling_queue_item_ids``，行内非行首项不再单独
   产出 entry。
2. ``CycleEntry`` 新增 ``sibling_queue_item_ids`` 字段，默认空 tuple，老调用点
   兼容。
3. 行首 entry 的 ``index`` 与 script_list 下标对齐，行内其他成员的 schedule 不
   再单独产出；行级 Schedule 仍挂在行首 QueueItem 上。

以下用例直接构造 dict 风格的 QueueConfig / ScriptConfig，跑 collect 纯函数，
不依赖 asyncio、Config 全局状态或网络。
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime
from typing import Any

from app.core.queue_cycle import (
    CycleEntry,
    collect_cycle_entries,
    resolve_next_run,
)
from app.core.task_manager import _ParallelGroupItem, _split_parallel_groups


class _FakeItem:
    """模拟 ConfigItem 的 get 接口：list[QueueItem] 用 dict 替代时仍然能
    ``item.get("Group", "Key")`` 取值。

    QueueItem 自身继承 ConfigBase，会拦截直接属性访问；这里只让 collect 走
    ``queue_item.get(...)``，所以 mock 一个带 get 的对象即可。
    """

    def __init__(self, info: dict[str, Any], schedule: dict[str, Any]) -> None:
        self._info = info
        self._schedule = schedule

    def get(self, group: str, key: str) -> Any:
        if group == "Info":
            return self._info.get(key)
        if group == "Schedule":
            return self._schedule.get(key)
        return None


def _make_queue(items: list[_FakeItem]) -> Any:
    """构造 collect_cycle_entries 期望的最小 queue 接口：``QueueItem.items()``。"""

    class _Queue:
        def __init__(self, by_id: dict[uuid.UUID, _FakeItem]) -> None:
            self.QueueItem = by_id

    by_id: dict[uuid.UUID, _FakeItem] = {}
    for fake in items:
        by_id[uuid.uuid4()] = fake
    return _Queue(by_id)


def _make_script_config(scripts: list[uuid.UUID]) -> Any:
    """构造 collect_cycle_entries 期望的最小 script_config 接口：
    ``script_config[uid].get("Info", "Name")``。
    """

    class _ScriptConfig:
        def __init__(self, by_uid: dict[uuid.UUID, _Script]) -> None:
            self._by_uid = by_uid

        def __contains__(self, uid: object) -> bool:
            return uid in self._by_uid

        def __getitem__(self, uid: uuid.UUID) -> _Script:
            return self._by_uid[uid]

    class _Script:
        def __init__(self, name: str) -> None:
            self._name = name

        def get(self, group: str, key: str) -> Any:
            if group == "Info" and key == "Name":
                return self._name
            return None

    by_uid = {uid: _Script(f"script-{i}") for i, uid in enumerate(scripts)}
    return _ScriptConfig(by_uid)


def _now() -> datetime:
    return datetime(2026, 9, 15, 8, 0, 0)


def _fixed_schedule() -> dict[str, Any]:
    """固定时间模式 8:00 触发、每日。"""

    return {
        "Enabled": True,
        "Mode": "fixed_time",
        "Days": [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
        "Time": "08:00",
        "IntervalMinutes": 480,
        "IntervalAnchor": "start",
        "NextRunAt": "",  # 空值哨兵 → 由 collect 用 Schedule 推算
    }


def _disabled_schedule() -> dict[str, Any]:
    """循环被关闭：整项不进 pending。"""

    return {**_fixed_schedule(), "Enabled": False}


class SingleRowParallelTest(unittest.TestCase):
    """连续 Parallel=True 合并成一行，行首带 sibling_queue_item_ids。"""

    def test_two_items_parallel_merge_into_one_row(self) -> None:
        s_a, s_b = uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": False}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_b), "Parallel": True}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(len(entries), 1)
        head = entries[0]
        self.assertEqual(head.script_id, str(s_a))
        self.assertEqual(head.index, 0)
        self.assertEqual(len(head.sibling_queue_item_ids), 1)
        # sibling id 是第二项的 QueueItem id（不能事先断言具体值，应为非空 str）
        sibling_id = head.sibling_queue_item_ids[0]
        self.assertIsInstance(sibling_id, str)
        self.assertEqual(len(sibling_id), 36)  # uuid4 str 长度

    def test_three_items_two_parallel_breaks_into_two_rows(self) -> None:
        """1-2 之间 Parallel=True、2-3 之间 Parallel=False → 行划分 [[0,1],[2]]。"""

        s_a, s_b, s_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": False}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_b), "Parallel": True}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_c), "Parallel": False}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b, s_c])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(len(entries), 2)
        # 行 1：a 与 b 并行
        self.assertEqual(entries[0].script_id, str(s_a))
        self.assertEqual(entries[0].index, 0)
        self.assertEqual(len(entries[0].sibling_queue_item_ids), 1)
        # 行 2：c 单独
        self.assertEqual(entries[1].script_id, str(s_c))
        self.assertEqual(entries[1].index, 2)
        self.assertEqual(entries[1].sibling_queue_item_ids, ())

    def test_three_items_all_serial_yields_three_rows(self) -> None:
        """全部 Parallel=False → 与旧行为一致：3 个独立 entry，无 sibling。"""

        s_a, s_b, s_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": False}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_b), "Parallel": False}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_c), "Parallel": False}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b, s_c])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(len(entries), 3)
        self.assertEqual([e.index for e in entries], [0, 1, 2])
        for e in entries:
            self.assertEqual(e.sibling_queue_item_ids, ())

    def test_long_parallel_group_at_end_of_queue(self) -> None:
        """a 跟 b/c/d 都并行（[F,T,T,T]）→ 一个 4 项的行；行末正常 flush。"""

        s_a, s_b, s_c, s_d = (uuid.uuid4() for _ in range(4))
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": False}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_b), "Parallel": True}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_c), "Parallel": True}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_d), "Parallel": True}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b, s_c, s_d])

        entries = collect_cycle_entries(queue, script_config, _now())

        # 整个 [a, b, c, d] 是 1 行 4 项
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].script_id, str(s_a))
        self.assertEqual(entries[0].index, 0)
        self.assertEqual(len(entries[0].sibling_queue_item_ids), 3)

    def test_long_parallel_group_at_start_of_queue(self) -> None:
        """并行组从队首开始，循环开始时立刻建立行首，下一次迭代继续追加。"""

        s_a, s_b, s_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": True}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_b), "Parallel": True}, _fixed_schedule()),
                _FakeItem({"ScriptId": str(s_c), "Parallel": False}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b, s_c])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(len(entries), 2)
        # 行 1：a 领头 + b
        self.assertEqual(entries[0].script_id, str(s_a))
        self.assertEqual(entries[0].index, 0)
        self.assertEqual(len(entries[0].sibling_queue_item_ids), 1)
        # 行 2：c 单独
        self.assertEqual(entries[1].script_id, str(s_c))
        self.assertEqual(entries[1].index, 2)


class DisabledRowDropsParallelTest(unittest.TestCase):
    """并行组内任一项 Schedule.Enabled=False 整行作废。"""

    def test_parallel_group_with_disabled_sibling_drops_row(self) -> None:
        """a 并行组 b，b 关了循环 → 不应产出任何 entry（a 也不进 pending）。"""

        s_a, s_b = uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem({"ScriptId": str(s_a), "Parallel": False}, _fixed_schedule()),
                _FakeItem(
                    {"ScriptId": str(s_b), "Parallel": True}, _disabled_schedule()
                ),
            ]
        )
        script_config = _make_script_config([s_a, s_b])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(entries, [])

    def test_parallel_group_with_disabled_head_drops_row(self) -> None:
        """a 并行组 b，a 关了循环 → 整行作废。"""

        s_a, s_b = uuid.uuid4(), uuid.uuid4()
        queue = _make_queue(
            [
                _FakeItem(
                    {"ScriptId": str(s_a), "Parallel": False}, _disabled_schedule()
                ),
                _FakeItem({"ScriptId": str(s_b), "Parallel": True}, _fixed_schedule()),
            ]
        )
        script_config = _make_script_config([s_a, s_b])

        entries = collect_cycle_entries(queue, script_config, _now())

        self.assertEqual(entries, [])


class CycleEntryBackcompatTest(unittest.TestCase):
    """默认 sibling_queue_item_ids = ()，老调用点零回归。"""

    def test_default_sibling_is_empty_tuple(self) -> None:
        entry = CycleEntry(
            queue_item_id="x",
            script_id="y",
            script_name="z",
            index=0,
            next_run_at=_now(),
            is_due=True,
        )
        self.assertEqual(entry.sibling_queue_item_ids, ())

    def test_resolve_next_run_returns_none_for_unscheduled(self) -> None:
        """空 Days 表示「不排期」 → resolve_next_run 返回 None。"""

        item = _FakeItem(
            {"ScriptId": str(uuid.uuid4()), "Parallel": False},
            {**_fixed_schedule(), "Days": []},
        )
        self.assertIsNone(resolve_next_run(item, _now()))


class CycleEntryGroupSplitTest(unittest.TestCase):
    """``_run_cycle_entry_group`` 的分组路径：过滤到行内下标后切分。

    复现修复后的调度逻辑：parallel_flags 按 script_list 绝对下标冻结，
    过滤到本行下标集合后 ``_split_parallel_groups`` 的首组必须恰好是整行，
    否则视为结构被改动。这里用与 ``_build_parallel_group_items`` 相同的
    取用口径（``index < len(flags) and flags[index]``）构造描述。
    """

    @staticmethod
    def _split_row(flags: list[bool], start: int, row_size: int) -> list[list[int]]:
        items = {
            index: _ParallelGroupItem(
                parallel=index < len(flags) and flags[index], conflict_keys=set()
            )
            for index in range(start, start + row_size)
        }
        return _split_parallel_groups(items)

    def test_row_not_at_queue_head_splits_as_whole_row(self) -> None:
        """行首绝对下标 >0：前面还有 2 个串行项，第 3~5 项构成并行行。"""

        # 绝对下标口径：行首 False、sibling True；行前项与行后项都不在行内
        flags = [False, False, False, True, True]
        groups = self._split_row(flags, start=2, row_size=3)

        self.assertEqual(groups, [[2, 3, 4]])

    def test_row_at_queue_head_splits_as_whole_row(self) -> None:
        """行首绝对下标 0：flags[0] 无论取值如何，组首恒新开一组。"""

        for head_flag in (False, True):
            with self.subTest(head_flag=head_flag):
                flags = [head_flag, True, True]
                groups = self._split_row(flags, start=0, row_size=3)

                self.assertEqual(groups, [[0, 1, 2]])

    def test_short_flags_do_not_leak_parallel_into_row(self) -> None:
        """flags 过短（旧数据/结构变动）时 sibling 取不到 True，首组缺员，
        与行内下标不一致——正是 ``group != indices`` 校验要拦的情形。"""

        flags = [True]  # 只有行首有标志，sibling 全部落到 False
        groups = self._split_row(flags, start=1, row_size=3)

        self.assertNotEqual(groups[0], [1, 2, 3])

    def test_sibling_false_breaks_row_into_two_groups(self) -> None:
        """行内某 sibling parallel=False：首组与整行下标不一致。"""

        flags = [False, True, False, True]
        groups = self._split_row(flags, start=0, row_size=4)

        self.assertNotEqual(groups[0], [0, 1, 2, 3])
        self.assertEqual(groups[0], [0, 1])


if __name__ == "__main__":
    unittest.main()
