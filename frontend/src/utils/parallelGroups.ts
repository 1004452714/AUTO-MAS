// 队列并行分组的前端口径实现，必须与后端
// app/core/task_manager.py: _split_parallel_groups 保持一致：
// 连续 parallel=true 的项并入同一组，组间串行执行。
export interface ParallelItem {
  /** 是否与上一项并行；后端字段可能为 null，非 true 一律视为串行 */
  parallel?: boolean | null
}

export interface ParallelKeyedItem extends ParallelItem {
  /** 唯一键，重算标志时按 id 映射 */
  id: string
}

export interface ParallelGroup<T extends ParallelItem> {
  items: T[]
}

/**
 * 按并行标志把顺序列表切分为执行批次；空输入返回空数组。
 *
 * 口径与后端 `app/core/task_manager.py: _split_parallel_groups` 一致：组首恒为
 * 新组，`parallel=true` 只表示「并入前一项所在的组」。
 *
 * 队列编辑页也用本函数，且**未选脚本的项照常按自身的 parallel 标志参与归组**
 * ——这一点是刻意的，因为 parallel 正是两个新增入口的语义区分：
 * - 「添加任务」新增项 `parallel=false`（后端 `Info_Parallel` 默认值，见
 *   `app/models/config.py:493`），因此自成一批，不会粘进末尾已有的批次；
 * - 「在本批次里再添加一个脚本」新增项被置为 `parallel=true` 并重排到该批次
 *   末项之后，因此就地并入该批次。
 *
 * 不要把未选脚本的项一律并入前一组：那会让「添加任务」的空项先粘进已有批次、
 * 选完脚本后再跳出来，看起来像是添加到了错误的组里。
 */
export function splitParallelGroups<T extends ParallelItem>(items: T[]): ParallelGroup<T>[] {
  const groups: ParallelGroup<T>[] = []
  for (const item of items) {
    const previous = groups[groups.length - 1]
    // 组首总是新开一组；parallel 只表示「并入前一项的组」
    if (previous && item.parallel === true) {
      previous.items.push(item)
    } else {
      groups.push({ items: [item] })
    }
  }
  return groups
}

/** 从分组结果反推每个 id 应有的并行标志：组首 false、其余 true */
export function recomputeParallelFlags<T extends ParallelKeyedItem>(
  groups: ParallelGroup<T>[]
): Map<string, boolean> {
  const flags = new Map<string, boolean>()
  for (const group of groups) {
    group.items.forEach((item, index) => {
      flags.set(item.id, index > 0)
    })
  }
  return flags
}
