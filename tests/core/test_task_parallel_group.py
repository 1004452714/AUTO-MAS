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

import asyncio
import unittest

from app.core.task_manager import (
    Task,
    TaskInfo,
    _ParallelGroupItem,
    _split_parallel_groups,
)


def make_items(*specs: tuple[bool, set[str]]) -> dict[int, _ParallelGroupItem]:
    """按 (parallel, conflict_keys) 序列构造绝对下标 → 描述的映射。"""

    return {
        index: _ParallelGroupItem(parallel=parallel, conflict_keys=keys)
        for index, (parallel, keys) in enumerate(specs)
    }


class SplitParallelGroupsTest(unittest.TestCase):
    """队列内并行组划分的纯函数行为。"""

    def test_all_serial_flags_degrade_to_single_item_groups(self) -> None:
        items = make_items((False, set()), (False, set()), (False, set()))

        groups = _split_parallel_groups(items)

        self.assertEqual(groups, [[0], [1], [2]])

    def test_consecutive_parallel_flags_merge_into_one_group(self) -> None:
        items = make_items(
            (False, set()),
            (True, {"s:B"}),
            (True, {"s:C"}),
            (False, set()),
        )

        groups = _split_parallel_groups(items)

        self.assertEqual(groups, [[0, 1, 2], [3]])

    def test_first_item_parallel_flag_starts_its_own_group(self) -> None:
        items = make_items((True, set()), (True, set()))

        groups = _split_parallel_groups(items)

        self.assertEqual(groups, [[0, 1]])

    def test_resource_conflict_stays_in_group_for_lock_queuing(self) -> None:
        items = make_items(
            (False, {"emu:A"}),
            (True, {"emu:A"}),
            (True, {"emu:B"}),
            (True, {"emu:B"}),
        )

        groups = _split_parallel_groups(items)

        # 共用模拟器实例的项不再拆组：留在同组内由资源锁排队，
        # 两条资源链才能并行（a1→b1 ‖ c1→c2）
        self.assertEqual(groups, [[0, 1, 2, 3]])

    def test_empty_items_yield_no_groups(self) -> None:
        self.assertEqual(_split_parallel_groups({}), [])

    def test_resume_offset_indices_stay_absolute(self) -> None:
        # resume_from_script_id 截断后，键是 script_list 的绝对下标（5 起），
        # 划分结果必须保持绝对下标，执行才不会错位到已跳过的前段脚本
        items = {
            5: _ParallelGroupItem(parallel=False, conflict_keys=set()),
            6: _ParallelGroupItem(parallel=True, conflict_keys=set()),
            7: _ParallelGroupItem(parallel=True, conflict_keys=set()),
        }

        groups = _split_parallel_groups(items)

        self.assertEqual(groups, [[5, 6, 7]])


class RunScriptGroupTimelineTest(unittest.IsolatedAsyncioTestCase):
    """并行组内资源锁排队的时间线行为。"""

    def _make_task(self) -> Task:
        task_info = TaskInfo(
            mode="AutoProxy",
            task_id="task-id",
            queue_id="queue-id",
            script_id=None,
            user_id=None,
        )
        return Task(task_info, [])

    async def test_resource_chains_queue_while_distinct_resources_overlap(self):
        """[a, b✓, c✓, d✓]（a/b 同模拟器 A，c/d 同模拟器 B）：
        a 与 c 并发，b 等 a，d 等 c——两条链各自串行、链间并行。"""

        events: list[tuple[str, int]] = []

        async def fake_run(index: int) -> None:
            events.append(("start", index))
            await asyncio.sleep(0.05)
            events.append(("end", index))

        task = self._make_task()
        task._run_script_at_index = fake_run

        items_by_index = make_items(
            (False, {"emu:A"}),
            (True, {"emu:A"}),
            (True, {"emu:B"}),
            (True, {"emu:B"}),
        )
        await task._run_script_group([0, 1, 2, 3], items_by_index)

        position = {event: i for i, event in enumerate(events)}
        # a 与 c 并发：c 在 a 结束前启动
        self.assertLess(position[("start", 2)], position[("end", 0)])
        # b 排队等 a：a 结束后 b 才启动
        self.assertGreater(position[("start", 1)], position[("end", 0)])
        # d 排队等 c
        self.assertGreater(position[("start", 3)], position[("end", 2)])
        # 全部完成
        self.assertEqual(len(events), 8)

    async def test_group_cancellation_stops_queued_item_cleanly(self):
        """用户停止时：持锁运行中的项与排队等锁的项都被取消，无残留。"""

        started: list[int] = []
        hang = asyncio.Event()

        async def fake_run(index: int) -> None:
            started.append(index)
            if index == 0:
                await hang.wait()

        task = self._make_task()
        task._run_script_at_index = fake_run

        items_by_index = make_items((False, {"k"}), (True, {"k"}))
        runner = asyncio.ensure_future(
            task._run_script_group([0, 1], items_by_index)
        )
        await asyncio.sleep(0.05)
        self.assertEqual(started, [0])

        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        # 排队项从未启动；取消传播后无协程残留
        self.assertEqual(started, [0])

    async def test_error_in_one_item_does_not_starve_queued_sibling(self):
        """一项抛异常时排队中的兄弟仍能拿到锁跑完，异常最后统一重抛。"""

        events: list[tuple[str, int]] = []

        async def fake_run(index: int) -> None:
            events.append(("start", index))
            if index == 0:
                raise RuntimeError("boom")
            await asyncio.sleep(0.05)
            events.append(("end", index))

        task = self._make_task()
        task._run_script_at_index = fake_run

        items_by_index = make_items((False, {"k"}), (True, {"k"}))
        with self.assertRaises(RuntimeError):
            await task._run_script_group([0, 1], items_by_index)

        # 兄弟项没有被异常饿死，仍排队执行完成
        self.assertIn(("end", 1), events)
        self.assertGreater(events.index(("start", 1)), events.index(("start", 0)))


if __name__ == "__main__":
    unittest.main()
