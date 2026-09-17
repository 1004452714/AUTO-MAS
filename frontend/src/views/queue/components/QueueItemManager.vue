<template>
  <a-card :title="t('queue.item.cardTitle')" class="queue-item-card">
    <template #extra>
      <a-space>
        <a-button type="primary" :loading="loading" :disabled="locked" @click="addQueueItem">
          <template #icon>
            <PlusOutlined />
          </template>
          {{ t('queue.item.add') }}
        </a-button>
      </a-space>
    </template>

    <!-- 使用vuedraggable替换a-table实现拖拽功能 -->
    <div class="draggable-table-container">
      <!-- 表头 -->
      <div class="draggable-table-header">
        <div class="header-cell drag-cell"></div>
        <div class="header-cell index-cell">{{ t('queue.item.colIndex') }}</div>
        <div class="header-cell script-cell">{{ t('queue.item.colScript') }}</div>
        <div v-if="showCycleConfig" class="header-cell cycle-cell">
          {{ t('queue.cycle.colConfig') }}
        </div>
        <div class="header-cell actions-cell">{{ t('queue.item.colActions') }}</div>
      </div>

      <!-- 拖拽内容区域 -->
      <draggable
        v-model="rows"
        group="queueItems"
        item-key="key"
        :animation="200"
        :disabled="loading || locked"
        ghost-class="ghost"
        chosen-class="chosen"
        drag-class="drag"
        handle=".drag-handle"
        class="draggable-container"
        @end="onDragEnd"
      >
        <template #item="{ element: row, index }">
          <div class="draggable-row" :class="{ 'row-dragging': loading }">
            <div class="row-cell drag-cell">
              <span
                class="drag-handle"
                :title="t('queue.item.dragSort')"
                :aria-label="t('queue.item.dragSort')"
              >
                <span class="drag-dots" aria-hidden="true"></span>
              </span>
            </div>
            <div class="row-cell index-cell">{{ index + 1 }}</div>
            <div class="row-cell script-cell">
              <div class="script-stack">
                <div v-for="item in row.items" :key="item.id" class="script-line">
                  <a-select
                    v-model:value="item.script"
                    size="small"
                    style="width: 200px"
                    class="script-select"
                    :placeholder="t('queue.item.selectScript')"
                    :options="scriptOptions"
                    :disabled="locked"
                    allow-clear
                    @change="updateQueueItemScript(item)"
                  />
                  <a-popconfirm
                    v-if="row.items.length > 1"
                    :title="t('queue.item.deleteConfirm')"
                    :ok-text="t('queue.ok')"
                    :cancel-text="t('queue.cancel')"
                    @confirm="removeScript(row, item)"
                  >
                    <a-button type="text" size="small" danger :disabled="locked">
                      <MinusCircleOutlined />
                    </a-button>
                  </a-popconfirm>
                </div>
              </div>
            </div>
            <div v-if="showCycleConfig" class="row-cell cycle-cell">
              <div class="cycle-panel">
                <div class="cycle-line">
                  <a-switch
                    v-model:checked="row.items[0].schedule.Enabled"
                    size="small"
                    @change="
                      saveSchedule(row.items[0], {
                        Enabled: row.items[0].schedule.Enabled,
                      })
                    "
                  />
                  <a-select
                    v-model:value="row.items[0].schedule.Mode"
                    size="small"
                    style="width: 104px"
                    :disabled="!row.items[0].schedule.Enabled"
                    @change="
                      saveSchedule(row.items[0], { Mode: row.items[0].schedule.Mode })
                    "
                  >
                    <a-select-option value="fixed_time">
                      {{ t('queue.cycle.modeFixed') }}
                    </a-select-option>
                    <a-select-option value="interval">
                      {{ t('queue.cycle.modeInterval') }}
                    </a-select-option>
                  </a-select>

                  <template v-if="row.items[0].schedule.Mode === 'interval'">
                    <a-input-number
                      v-model:value="row.items[0].schedule.IntervalMinutes"
                      size="small"
                      style="width: 104px"
                      :min="1"
                      :max="10080"
                      :disabled="!row.items[0].schedule.Enabled"
                      :addon-after="t('queue.cycle.minuteUnit')"
                      @blur="saveInterval(row.items[0])"
                      @press-enter="saveInterval(row.items[0])"
                    />
                    <a-select
                      v-model:value="row.items[0].schedule.IntervalAnchor"
                      size="small"
                      style="width: 150px"
                      :disabled="!row.items[0].schedule.Enabled"
                      @change="
                        saveSchedule(row.items[0], {
                          IntervalAnchor: row.items[0].schedule.IntervalAnchor,
                        })
                      "
                    >
                      <a-select-option value="start">
                        {{ t('queue.cycle.anchorStart') }}
                      </a-select-option>
                      <a-select-option value="finish">
                        {{ t('queue.cycle.anchorFinish') }}
                      </a-select-option>
                    </a-select>
                  </template>
                  <template v-else>
                    <a-time-picker
                      v-model:value="row.items[0].scheduleTimeValue"
                      format="HH:mm"
                      size="small"
                      style="width: 104px"
                      :placeholder="t('queue.time.selectTime')"
                      :disabled="!row.items[0].schedule.Enabled"
                      :allow-clear="false"
                      @change="saveScheduleTime(row.items[0])"
                    />
                    <a-select
                      v-model:value="row.items[0].schedule.Days"
                      mode="multiple"
                      size="small"
                      style="min-width: 168px"
                      :placeholder="t('queue.time.selectDays')"
                      :disabled="!row.items[0].schedule.Enabled"
                      :max-tag-count="3"
                      @change="
                        saveSchedule(row.items[0], { Days: row.items[0].schedule.Days })
                      "
                    >
                      <a-select-option value="Monday">{{ t('queue.time.Monday') }}</a-select-option>
                      <a-select-option value="Tuesday">
                        {{ t('queue.time.Tuesday') }}
                      </a-select-option>
                      <a-select-option value="Wednesday">
                        {{ t('queue.time.Wednesday') }}
                      </a-select-option>
                      <a-select-option value="Thursday">
                        {{ t('queue.time.Thursday') }}
                      </a-select-option>
                      <a-select-option value="Friday">{{ t('queue.time.Friday') }}</a-select-option>
                      <a-select-option value="Saturday">
                        {{ t('queue.time.Saturday') }}
                      </a-select-option>
                      <a-select-option value="Sunday">{{ t('queue.time.Sunday') }}</a-select-option>
                    </a-select>
                  </template>
                </div>

                <div class="cycle-line cycle-next-line">
                  <span class="cycle-next-text">
                    {{ t('queue.cycle.nextRun') }}
                    {{ formatNextRun(row.items[0].schedule.NextRunAt) }}
                  </span>
                  <a-button
                    type="link"
                    size="small"
                    :disabled="!row.items[0].schedule.Enabled"
                    @click="runOnce(row.items[0])"
                  >
                    {{ t('queue.cycle.runOnce') }}
                  </a-button>
                </div>
              </div>
            </div>
            <div class="row-cell actions-cell">
              <a-space>
                <a-tooltip :title="t('queue.item.addScriptTip')">
                  <a-button :disabled="locked || loading" @click="appendScript(row)">
                    <PlusOutlined />
                    {{ t('queue.item.addScript') }}
                  </a-button>
                </a-tooltip>
                <a-popconfirm
                  v-if="row.items.length > 1"
                  :title="t('queue.item.deleteRowConfirm')"
                  :ok-text="t('queue.ok')"
                  :cancel-text="t('queue.cancel')"
                  @confirm="deleteRow(row)"
                >
                  <a-button size="middle" danger :disabled="locked">
                    <DeleteOutlined />
                    {{ t('queue.del') }}
                  </a-button>
                </a-popconfirm>
                <a-popconfirm
                  v-else
                  :title="t('queue.item.deleteConfirm')"
                  :ok-text="t('queue.ok')"
                  :cancel-text="t('queue.cancel')"
                  @confirm="removeScript(row, row.items[0])"
                >
                  <a-button size="middle" danger :disabled="locked">
                    <DeleteOutlined />
                    {{ t('queue.del') }}
                  </a-button>
                </a-popconfirm>
              </a-space>
            </div>
          </div>
        </template>
      </draggable>

      <!-- 空状态 -->
      <div v-if="rows.length === 0" class="empty-state">
        <div class="empty-content">
          <img src="@/assets/NoData.png" :alt="t('queue.noData')" class="empty-image" />
        </div>
      </div>
    </div>
  </a-card>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { onMounted, ref, nextTick, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  DeleteOutlined,
  MinusCircleOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'
import draggable from 'vuedraggable'
import dayjs from 'dayjs'
import { Service } from '@/api'
import { splitParallelGroups } from '@/utils/parallelGroups'

const { t } = useI18n()
const logger = window.electronAPI.getLogger('队列项管理')

// Props
interface Props {
  queueId: string
  queueItems: any[]
  showCycleConfig?: boolean
  // 队列正在循环运行：增删、排序、换脚本会被后端拦下，循环周期仍可改
  locked?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  showCycleConfig: false,
  locked: false,
})

// Emits
const emit = defineEmits<{
  refresh: []
}>()

// 响应式数据
const loading = ref(false)
const isDraggingQueueItem = ref(false)

// 选项数据
const scriptOptions = ref<Array<{ label: string; value: string | null }>>([])

// 后端 NextRunAt 的空值哨兵，表示「尚未推算」；见 app/utils/constants.py
const CYCLE_EMPTY_TIME = '2000-01-01 00:00:00'

// 循环调度的默认值，与后端 QueueItem 的配置项保持一致
const CYCLE_SCHEDULE_DEFAULTS = {
  Enabled: true,
  Mode: 'fixed_time',
  Days: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
  Time: '00:00',
  IntervalMinutes: 480,
  IntervalAnchor: 'start',
  NextRunAt: CYCLE_EMPTY_TIME,
}

// 补齐循环配置并派生时间选择器需要的 dayjs 值。
// savedIntervalMinutes 记录“上次成功保存”的间隔分钟数，用于校验失败时回填，
// 而不是硬编码默认值 480。
const withCycleSchedule = (items: any[]) =>
  items.map(item => {
    const schedule = { ...CYCLE_SCHEDULE_DEFAULTS, ...(item.schedule || {}) }
    return {
      ...item,
      schedule,
      scheduleTimeValue: parseTimeString(schedule.Time),
      savedIntervalMinutes: schedule.IntervalMinutes,
    }
  })

// 计算属性 - 使用props传入的数据
// 一行 = 一个并行批次（splitParallelGroups 把相邻 Parallel=true 的项合并），
// 单脚本行就是当前所有 QueueItem 的退化情形
interface QueueRow {
  key: string
  items: any[]
}

const buildRows = (items: any[]): QueueRow[] =>
  splitParallelGroups(items).map(g => ({ key: g.items[0].id, items: g.items }))

const rows = ref<QueueRow[]>(buildRows(withCycleSchedule(props.queueItems)))

// 监听props变化
watch(
  () => props.queueItems,
  newQueueItems => {
    if (!isDraggingQueueItem.value) {
      rows.value = buildRows(withCycleSchedule(newQueueItems))
    }
  },
  { deep: true }
)

// 时间字符串 "HH:mm" 与时间选择器的 dayjs 值互转
const parseTimeString = (timeString: string) => {
  const [hours = 0, minutes = 0] = String(timeString || '00:00')
    .split(':')
    .map(Number)
  return dayjs().hour(hours).minute(minutes).second(0).millisecond(0)
}

const formatTimeValue = (timeValue: any) => {
  if (!timeValue) return '00:00'
  return dayjs.isDayjs(timeValue) ? timeValue.format('HH:mm') : dayjs(timeValue).format('HH:mm')
}

// 空值哨兵表示还没推算过，展示成「待排期」而不是 2000 年
const formatNextRun = (nextRunAt: string) =>
  !nextRunAt || nextRunAt === CYCLE_EMPTY_TIME ? t('queue.cycle.notScheduled') : nextRunAt

// 保存循环调度配置。这里不 emit refresh：本地 record 已是最新值，
// 整表刷新反而会把用户正在编辑的输入顶掉。返回是否保存成功，供调用方
// 决定要不要把本地值当作“已确认”的基线（例如 savedIntervalMinutes）。
const saveSchedule = async (record: any, data: Record<string, any>): Promise<boolean> => {
  try {
    const response = await Service.updateItemApiQueueItemUpdatePost({
      queueId: props.queueId,
      queueItemId: record.id,
      data: { Schedule: data },
    })

    if (response.code !== 200) {
      message.error(
        t('queue.toast.scheduleUpdateFailed', {
          error: response.message || t('queue.toast.unknownError'),
        })
      )
      emit('refresh')
      return false
    }
    return true
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`更新循环调度失败: ${errorMsg}`)
    message.error(t('queue.toast.scheduleUpdateFailed', { error: errorMsg }))
    emit('refresh')
    return false
  }
}

// 失焦/回车才保存，而不是每次键入都存盘（InputNumber 的 change 事件逐字符触发）。
// 校验失败（非 1..10080 的整数，含清空后的 null）时回填上次成功保存的值，不发请求；
// 值未变化时同样不发请求。
const saveInterval = async (record: any) => {
  const minutes = Number(record.schedule.IntervalMinutes)
  const isValid = Number.isInteger(minutes) && minutes >= 1 && minutes <= 10080

  if (!isValid) {
    record.schedule.IntervalMinutes = record.savedIntervalMinutes
    return
  }

  if (minutes === record.savedIntervalMinutes) {
    record.schedule.IntervalMinutes = minutes
    return
  }

  record.schedule.IntervalMinutes = minutes
  const success = await saveSchedule(record, { IntervalMinutes: minutes })
  if (success) {
    record.savedIntervalMinutes = minutes
  }
}

// 周几按星期顺序存盘，和定时列表保持一致
const DAY_ORDER = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

const saveDays = async (record: any) => {
  const days = [...(record.schedule.Days || [])].sort(
    (a: string, b: string) => DAY_ORDER.indexOf(a) - DAY_ORDER.indexOf(b)
  )
  record.schedule.Days = days
  await saveSchedule(record, { Days: days })
}

const saveScheduleTime = async (record: any) => {
  const timeString = formatTimeValue(record.scheduleTimeValue)
  record.schedule.Time = timeString
  await saveSchedule(record, { Time: timeString })
}

// 把下次运行时间提到当前，循环下一轮就会挑中它
const runOnce = async (record: any) => {
  const nextRunAt = dayjs().format('YYYY-MM-DD HH:mm:ss')
  record.schedule.NextRunAt = nextRunAt
  await saveSchedule(record, { NextRunAt: nextRunAt })
}

// 加载脚本选项
const loadOptions = async () => {
  try {
    logger.info('开始加载脚本选项...')
    // 使用正确的API获取脚本下拉框选项
    const scriptsResponse = await Service.getScriptComboxApiInfoComboxScriptPost()
    logger.debug(`脚本API响应: ${JSON.stringify(scriptsResponse)}`)

    if (scriptsResponse.code === 200) {
      logger.debug(`脚本API响应数据: ${JSON.stringify(scriptsResponse.data)}`)
      // 直接使用接口返回的combox选项
      scriptOptions.value = scriptsResponse.data || []
      logger.debug(`处理后的脚本选项: ${JSON.stringify(scriptOptions.value)}`)
    } else {
      logger.error(`脚本API响应错误: ${JSON.stringify(scriptsResponse)}`)
    }
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`加载脚本选项失败: ${errorMsg}`)
  }
}

// 更新队列项脚本
const updateQueueItemScript = async (record: any) => {
  try {
    loading.value = true

    const response = await Service.updateItemApiQueueItemUpdatePost({
      queueId: props.queueId,
      queueItemId: record.id,
      data: {
        Info: {
          ScriptId: record.script,
        },
      },
    })

    if (response.code === 200) {
      emit('refresh')
    } else {
      message.error(
        t('queue.toast.scriptUpdateFailed', {
          error: response.message || t('queue.toast.unknownError'),
        })
      )
    }
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`更新脚本失败: ${errorMsg}`)
    message.error(t('queue.toast.updateScriptFailed', { error: errorMsg }))
  } finally {
    loading.value = false
  }
}

// 添加队列项
const addQueueItem = async () => {
  try {
    loading.value = true

    // 直接创建队列项，默认ScriptId为null（未选择）
    const createResponse = await Service.addItemApiQueueItemAddPost({
      queueId: props.queueId,
    })

    if (createResponse.code === 200 && createResponse.queueItemId) {
      emit('refresh')
    } else {
      message.error(
        t('queue.toast.addTaskFailed', {
          error: createResponse.message || t('queue.toast.unknownError'),
        })
      )
    }
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`添加任务失败: ${errorMsg}`)
    message.error(t('queue.toast.addTaskFailed2', { error: errorMsg }))
  } finally {
    loading.value = false
  }
}

// 删除队列项
const deleteQueueItem = async (itemId: string) => {
  try {
    const response = await Service.deleteItemApiQueueItemDeletePost({
      queueId: props.queueId,
      queueItemId: itemId,
    })

    if (response.code === 200) {
      // 确保删除后刷新数据
      emit('refresh')
    } else {
      message.error(
        t('queue.toast.deleteItemFailed', {
          error: response.message || t('queue.toast.unknownError'),
        })
      )
    }
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`删除队列项失败: ${errorMsg}`)
    message.error(t('queue.toast.deleteItemFailed', { error: errorMsg }))
  }
}

// 拖拽结束处理函数
const onDragEnd = async (evt: any) => {
  // 如果位置没有变化，直接返回
  if (evt.oldIndex === evt.newIndex) {
    return
  }

  isDraggingQueueItem.value = true

  try {
    loading.value = true

    // 构造排序后的ID列表（按行的展开顺序，保持后端 script_list 下标不变）
    const sortedIds = rows.value.flatMap(r => r.items.map(i => i.id))

    // 调用排序API
    const response = await Service.reorderItemApiQueueItemOrderPost({
      queueId: props.queueId,
      indexList: sortedIds,
    })

    if (response.code === 200) {
      // 刷新数据以确保与服务器同步
      emit('refresh')
    } else {
      message.error(
        t('queue.toast.reorderFailed', { error: response.message || t('queue.toast.unknownError') })
      )
      // 如果失败，刷新数据恢复原状态
      emit('refresh')
    }
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`拖拽排序失败: ${errorMsg}`)
    message.error(t('queue.toast.reorderFailed', { error: errorMsg }))
    // 如果失败，刷新数据恢复原状态
    emit('refresh')
  } finally {
    loading.value = false
    nextTick(() => {
      isDraggingQueueItem.value = false
    })
  }
}

// 在指定行末尾追加一个脚本（与本行其他脚本并行）
const appendScript = async (row: QueueRow) => {
  if (props.locked) return
  // 创建成功后的任何一步失败都要把空项删掉，不让队列残留未配置的项
  let newId: string | null = null
  const rollbackNewItem = async () => {
    if (!newId) return
    try {
      await Service.deleteItemApiQueueItemDeletePost({
        queueId: props.queueId,
        queueItemId: newId,
      })
    } catch (error: any) {
      const msg = error instanceof Error ? error.message : String(error)
      logger.warn(`回滚新建队列项失败: ${msg}`)
    }
  }
  try {
    loading.value = true
    // 1. 新建一个空脚本项
    const createResponse = await Service.addItemApiQueueItemAddPost({
      queueId: props.queueId,
    })
    if (createResponse.code !== 200 || !createResponse.queueItemId) {
      message.error(
        t('queue.toast.addTaskFailed', {
          error: createResponse.message || t('queue.toast.unknownError'),
        })
      )
      return
    }
    newId = createResponse.queueItemId

    // 2. parallel=true 表示「并入前一项所在组」，标志必须写在新项上；
    //    行首保持 false，行边界才不会变
    const flagResponse = await Service.updateItemApiQueueItemUpdatePost({
      queueId: props.queueId,
      queueItemId: newId,
      data: { Info: { Parallel: true } },
    })
    if (flagResponse.code !== 200) {
      message.error(
        t('queue.toast.updateScriptFailed', {
          error: flagResponse.message || t('queue.toast.unknownError'),
        })
      )
      await rollbackNewItem()
      return
    }

    // 3. 把新 id 插到本行末项之后：其余项位置不变，行边界与批次顺序都不动
    const currentOrder = rows.value.flatMap(r => r.items.map(i => i.id))
    const insertAt = currentOrder.indexOf(row.items[row.items.length - 1].id) + 1
    const order = [
      ...currentOrder.slice(0, insertAt),
      newId,
      ...currentOrder.slice(insertAt),
    ]
    const reorderResponse = await Service.reorderItemApiQueueItemOrderPost({
      queueId: props.queueId,
      indexList: order,
    })
    if (reorderResponse.code !== 200) {
      message.error(
        t('queue.toast.reorderFailed', {
          error: reorderResponse.message || t('queue.toast.unknownError'),
        })
      )
      await rollbackNewItem()
      return
    }
    emit('refresh')
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`追加脚本失败: ${errorMsg}`)
    message.error(t('queue.toast.addTaskFailed2', { error: errorMsg }))
    await rollbackNewItem()
  } finally {
    loading.value = false
  }
}

// 删除行内某个脚本
const removeScript = async (row: QueueRow, item: any) => {
  if (props.locked) return
  // 删组首前先把新组首 Parallel 置 false，否则删完它会被并进上一行；
  // 后续删除失败时要把标志回滚成 true，避免留下 refresh 也校正不了的行边界
  const isHeadOfMultiRow = item.id === row.items[0].id && row.items.length > 1
  const newLeader = isHeadOfMultiRow ? row.items[1] : null
  const restoreLeader = async () => {
    if (!newLeader) return
    try {
      await Service.updateItemApiQueueItemUpdatePost({
        queueId: props.queueId,
        queueItemId: newLeader.id,
        data: { Info: { Parallel: true } },
      })
    } catch (error: any) {
      const msg = error instanceof Error ? error.message : String(error)
      logger.warn(`新组首 Parallel 回滚失败: ${msg}`)
    }
  }
  if (newLeader) {
    try {
      const r = await Service.updateItemApiQueueItemUpdatePost({
        queueId: props.queueId,
        queueItemId: newLeader.id,
        data: { Info: { Parallel: false } },
      })
      if (r.code !== 200) {
        message.error(
          t('queue.toast.updateScriptFailed', {
            error: r.message || t('queue.toast.unknownError'),
          })
        )
        return
      }
    } catch (error: any) {
      const errorMsg = error instanceof Error ? error.message : String(error)
      logger.error(`更新新组首并行标志失败: ${errorMsg}`)
      message.error(t('queue.toast.updateScriptFailed', { error: errorMsg }))
      return
    }
  }
  try {
    const response = await Service.deleteItemApiQueueItemDeletePost({
      queueId: props.queueId,
      queueItemId: item.id,
    })
    if (response.code === 200) {
      emit('refresh')
    } else {
      await restoreLeader()
      message.error(
        t('queue.toast.deleteItemFailed', {
          error: response.message || t('queue.toast.unknownError'),
        })
      )
    }
  } catch (error: any) {
    await restoreLeader()
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`删除脚本失败: ${errorMsg}`)
    message.error(t('queue.toast.deleteItemFailed', { error: errorMsg }))
  }
}

// 删除整行（并行组）
const deleteRow = async (row: QueueRow) => {
  if (props.locked) return
  try {
    for (const item of row.items) {
      const response = await Service.deleteItemApiQueueItemDeletePost({
        queueId: props.queueId,
        queueItemId: item.id,
      })
      if (response.code !== 200) {
        message.error(
          t('queue.toast.deleteItemFailed', {
            error: response.message || t('queue.toast.unknownError'),
          })
        )
      }
    }
    emit('refresh')
  } catch (error: any) {
    const errorMsg = error instanceof Error ? error.message : String(error)
    logger.error(`删除批次失败: ${errorMsg}`)
    message.error(t('queue.toast.deleteItemFailed', { error: errorMsg }))
  }
}

// 初始化
onMounted(() => {
  loadOptions()
})
</script>

<style scoped>
.queue-item-card {
  margin-bottom: 24px;
}

.queue-item-card :deep(.ant-card-head-title) {
  font-size: 18px;
  font-weight: 600;
}

/* 操作按钮布局 */
:deep(.ant-btn) {
  min-width: auto;
  height: 32px;
  padding: 0 8px;
  font-size: 14px;
  line-height: 1.5;
}

:deep(.ant-space) {
  gap: 6px !important;
}

:deep(.ant-space-item) {
  margin-right: 6px !important;
}

/* 按钮图标样式调整 */
:deep(.ant-btn .anticon) {
  font-size: 14px;
}

/* 队列项列表样式 */
.queue-items-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.queue-item-row {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  background: var(--ant-color-bg-container);
  border: 1px solid var(--ant-color-border);
  border-radius: 6px;
  transition: all 0.2s ease;
}

.queue-item-row:hover {
  border-color: var(--ant-color-primary);
  box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
}

.item-left {
  flex: 0 0 120px;
}

.item-index {
  font-weight: 500;
  color: var(--ant-color-text);
  font-size: 14px;
}

.item-center {
  flex: 1;
  padding: 0 16px;
}

.script-name {
  color: var(--ant-color-text);
  font-size: 14px;
}

.item-right {
  flex: 0 0 auto;
  display: flex;
  gap: 8px;
}

/* 拖拽表格样式 */
.draggable-table-container {
  width: 100%;
  border: 1px solid var(--ant-color-border);
  border-radius: 6px;
  overflow: hidden;
}

.draggable-table-header {
  display: flex;
  background-color: var(--ant-color-fill-quaternary);
  border-bottom: 1px solid var(--ant-color-border);
}

.header-cell {
  padding: 12px 16px;
  font-weight: 600;
  color: var(--ant-color-text);
  text-align: center;
  border-right: 1px solid var(--ant-color-border);
}

.header-cell:last-child {
  border-right: none;
}

.index-cell {
  width: 80px;
  min-width: 80px;
  max-width: 80px;
}

.drag-cell {
  width: 36px;
  min-width: 36px;
  max-width: 36px;
}

.script-cell {
  flex: 1.5 1 320px;
  min-width: 200px;
}

.actions-cell {
  /* 容纳「添加 + 删除」两个 middle 按钮，避免溢出表格 */
  width: 224px;
  min-width: 224px;
  max-width: 224px;
}

.draggable-container {
  min-height: 60px;
}

.draggable-row {
  display: flex;
  /* 单元格拉伸到整行高，竖线边框才能与行的上下横线连通 */
  align-items: stretch;
  background: var(--ant-color-bg-container);
  border-bottom: 1px solid var(--ant-color-border);
  transition: all 0.2s ease;
  cursor: default;
}

.draggable-row:last-child {
  border-bottom: none;
}

.draggable-row:hover {
  background-color: var(--ant-color-fill-quaternary);
}

.draggable-row.row-dragging {
  cursor: not-allowed;
}

.row-cell {
  padding: 12px 16px;
  text-align: center;
  border-right: 1px solid var(--ant-color-border);
  display: flex;
  align-items: center;
  justify-content: center;
}

.row-cell:last-child {
  border-right: none;
}

.row-cell.index-cell {
  width: 80px;
  min-width: 80px;
  max-width: 80px;
  font-weight: 500;
  color: var(--ant-color-text-secondary);
}

.row-cell.drag-cell {
  width: 36px;
  min-width: 36px;
  max-width: 36px;
}

.header-cell.cycle-cell,
.row-cell.cycle-cell {
  flex: 1 1 400px;
  min-width: 0;
}

.cycle-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.cycle-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.cycle-next-line {
  font-size: 12px;
  color: var(--ant-color-text-secondary);
}

.cycle-next-text {
  font-variant-numeric: tabular-nums;
}

.row-cell.script-cell {
  flex: 1.5 1 320px;
  min-width: 200px;
}

.row-cell.actions-cell {
  width: 224px;
  min-width: 224px;
  max-width: 224px;
}

/* 脚本列多脚本平铺：同一行的脚本横向排列，超出列宽才换行；保持列内居中 */
.script-stack {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
}

.script-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 拖拽状态样式 */
.ghost {
  opacity: 0 !important;
  background: transparent !important;
  border-color: transparent !important;
  box-shadow: none !important;
}

.chosen {
  cursor: grabbing !important;
}

.drag {
  transform: rotate(3deg);
  opacity: 1 !important;
}

.drag .draggable-row {
  opacity: 1 !important;
  transition: none !important;
}

.drag-handle {
  width: 16px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--ant-color-text-tertiary);
  background: transparent;
  border: none;
  cursor: grab;
  user-select: none;
}

.drag-handle:active {
  cursor: grabbing;
}

.drag-dots {
  width: 10px;
  height: 16px;
  display: block;
  background-image: radial-gradient(currentColor 1.2px, transparent 1.2px);
  background-size: 5px 5px;
  opacity: 0.65;
}

.drag-handle:hover .drag-dots {
  opacity: 0.85;
}

/* 空状态样式 */
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}

.empty-content {
  display: flex;
  justify-content: center;
}

.empty-image {
  max-width: 200px;
  height: auto;
  opacity: 0.9;
  filter: drop-shadow(0 8px 24px rgba(0, 0, 0, 0.1));
  transition: all 0.3s ease;
  position: relative;
  z-index: 1;
}

.empty-image:hover {
  transform: translateY(-4px);
  filter: drop-shadow(0 12px 32px rgba(0, 0, 0, 0.15));
}

/* 响应式设计 */
@media (max-width: 1200px) {
  .queue-items-grid {
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }
}

@media (max-width: 768px) {
  .queue-items-grid {
    grid-template-columns: 1fr;
  }

  .queue-item-card-item {
    padding: 12px;
  }

  .draggable-row {
    flex-direction: column;
    align-items: stretch;
  }

  .row-cell,
  .header-cell {
    border-right: none;
    border-bottom: 1px solid var(--ant-color-border);
  }

  .row-cell:last-child,
  .header-cell:last-child {
    border-bottom: none;
  }

  .index-cell,
  .drag-cell,
  .script-cell,
  .days-cell,
  .actions-cell {
    width: 100% !important;
    min-width: auto !important;
    max-width: none !important;
  }
}

/* 标签样式 */
:deep(.ant-tag) {
  margin: 0;
  border-radius: 4px;
}

/* 脚本下拉框样式 - 使用与TimeSetManager.vue状态下拉框相同的样式 */
.script-select :deep(.ant-select-selector) {
  background: transparent !important;
  border: none !important;
  padding: 0 6px !important;
  min-height: 28px !important;
  line-height: 26px !important;
  box-shadow: none !important;
  text-align: center;
}

.script-select :deep(.ant-select-selection-item) {
  line-height: 26px !important;
  color: var(--ant-color-text) !important;
  font-weight: 500;
  padding: 0;
  margin: 0;
}

.script-select :deep(.ant-select-selection-placeholder) {
  line-height: 26px !important;
  color: var(--ant-color-text-placeholder) !important;
  padding: 0;
  margin: 0;
}

.script-select :deep(.ant-select-clear) {
  display: none !important;
}

.script-select :deep(.ant-select-selection-search) {
  margin: 0 !important;
  padding: 0;
}

.script-select :deep(.ant-select-selection-search-input) {
  padding: 0 !important;
  margin: 0 !important;
  height: 26px !important;
}

.script-select:hover :deep(.ant-select-selector) {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
}

.script-select:focus-within :deep(.ant-select-selector),
.script-select.ant-select-focused :deep(.ant-select-selector) {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
  outline: none !important;
}

.script-select :deep(.ant-select-selector):focus,
.script-select :deep(.ant-select-selector):focus-within {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
  outline: none !important;
  cursor: default !important;
}

/* 下拉箭头样式 */
.script-select :deep(.ant-select-arrow) {
  right: 4px;
  color: var(--ant-color-text-tertiary);
  font-size: 10px;
}

.script-select :deep(.ant-select-arrow:hover) {
  color: var(--ant-color-primary);
}

/* 自定义下拉框样式 - 增加下拉菜单宽度 */
.script-select :deep(.ant-select-dropdown) {
  min-width: 200px !important;
  max-width: 300px !important;
}

.script-select :deep(.ant-select-item) {
  padding: 8px 12px !important;
}
</style>
