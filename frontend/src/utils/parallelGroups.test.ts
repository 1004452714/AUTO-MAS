import { describe, expect, it } from 'vitest'

import { recomputeParallelFlags, splitParallelGroups } from './parallelGroups'

const buildItems = (flags: Array<boolean | null | undefined>) =>
  flags.map((parallel, index) => ({ id: `item-${index + 1}`, parallel }))

describe('splitParallelGroups', () => {
  it('空输入返回空数组', () => {
    expect(splitParallelGroups([])).toEqual([])
  })

  it('首项无论标志如何都是组首', () => {
    const groups = splitParallelGroups(buildItems([true, true]))
    expect(groups).toHaveLength(1)
    expect(groups[0].items.map(item => item.id)).toEqual(['item-1', 'item-2'])
  })

  it('连续 parallel=true 归入同组，false 断开', () => {
    const groups = splitParallelGroups(buildItems([false, true, true, false, true]))
    expect(groups.map(group => group.items.map(item => item.id))).toEqual([
      ['item-1', 'item-2', 'item-3'],
      ['item-4', 'item-5'],
    ])
  })

  it('null/undefined 与 false 同样断开（与后端口径一致）', () => {
    const groups = splitParallelGroups(buildItems([false, null, undefined]))
    expect(groups).toHaveLength(3)
  })

  it('单项队列返回单组', () => {
    const groups = splitParallelGroups(buildItems([false]))
    expect(groups).toHaveLength(1)
    expect(groups[0].items).toHaveLength(1)
  })
})

describe('recomputeParallelFlags', () => {
  it('组首 false、其余 true', () => {
    const groups = splitParallelGroups(buildItems([false, true, true, false, true]))
    const flags = recomputeParallelFlags(groups)
    expect(flags.get('item-1')).toBe(false)
    expect(flags.get('item-2')).toBe(true)
    expect(flags.get('item-3')).toBe(true)
    expect(flags.get('item-4')).toBe(false)
    expect(flags.get('item-5')).toBe(true)
  })

  it('拖拽跨组移动后按新分组重算标志', () => {
    // 模拟把 item-4 拖到第一组末尾后的新分组
    const regrouped = [
      {
        items: [
          { id: 'item-1', parallel: false },
          { id: 'item-2', parallel: true },
          { id: 'item-4', parallel: true },
        ],
      },
      { items: [{ id: 'item-3', parallel: false }] },
    ]
    const flags = recomputeParallelFlags(regrouped)
    expect(flags.get('item-1')).toBe(false)
    expect(flags.get('item-2')).toBe(true)
    expect(flags.get('item-4')).toBe(true)
    expect(flags.get('item-3')).toBe(false)
  })
})

describe('队列编辑页归组（含未选脚本的项）', () => {
  // 空字符串表示未选择脚本；parallel 显式给出，避免依赖下标推断
  const buildMixed = (rows: Array<[string, boolean]>) =>
    rows.map(([script, parallel], index) => ({ id: `item-${index + 1}`, script, parallel }))

  it('空输入返回空数组', () => {
    expect(splitParallelGroups([])).toEqual([])
  })

  it('「添加任务」新增的空项 parallel=false，自成一批，不粘进末尾已有批次', () => {
    // [A(false), B(true), 新增(false)]：A、B 一批，新增项独立占一行
    const items = buildMixed([
      ['A', false],
      ['B', true],
      ['', false],
    ])
    const groups = splitParallelGroups(items)

    expect(groups.map(group => group.items.map(item => item.id))).toEqual([
      ['item-1', 'item-2'],
      ['item-3'],
    ])
  })

  it('「在本批次里再添加一个脚本」的空项 parallel=true，就地并入该批次', () => {
    // [A(false), B(true), 新槽位(true)]：三者在同一行
    const items = buildMixed([
      ['A', false],
      ['B', true],
      ['', true],
    ])
    const groups = splitParallelGroups(items)

    expect(groups.map(group => group.items.map(item => item.id))).toEqual([
      ['item-1', 'item-2', 'item-3'],
    ])
  })

  it('空项按自身标志断行，与它是否已选脚本无关', () => {
    const items = buildMixed([
      ['A', false],
      ['', false],
      ['', true],
    ])
    const groups = splitParallelGroups(items)

    expect(groups.map(group => group.items.map(item => item.id))).toEqual([
      ['item-1'],
      ['item-2', 'item-3'],
    ])
  })

  it('空项出现在最前面时自己占一行', () => {
    const items = buildMixed([
      ['', true],
      ['A', false],
    ])
    const groups = splitParallelGroups(items)

    expect(groups.map(group => group.items.map(item => item.id))).toEqual([['item-1'], ['item-2']])
  })

  it('全部未配置时空项按各自标志归组', () => {
    const items = buildMixed([
      ['', false],
      ['', true],
    ])
    const groups = splitParallelGroups(items)

    expect(groups.map(group => group.items.map(item => item.id))).toEqual([['item-1', 'item-2']])
  })
})
