import { onUnmounted, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import { message } from 'ant-design-vue'
import { Service, TaskCreateIn } from '@/api'
import { useWebSocket } from '@/composables/useWebSocket'
import { realtimeSnapshotApi } from '@/services/realtimeSnapshotApi'
import {
  WS_TASK_COMPLETED,
  WS_TASK_LOG_UPDATED,
  WS_TASK_NOTICE,
  type WSTaskCompletedData,
  type WSTaskLogUpdatedData,
  type WSTaskNoticeData,
} from '@/services/websocket/types'

/** 单次更新日志的缓冲上限 */
const UPDATE_LOG_MAX_CHARS = 200_000
/** 单次更新的兜底时限（实测一次增量约 10 GB，给足 30 分钟） */
const UPDATE_TIMEOUT_MS = 30 * 60 * 1000

/**
 * BetterGI 用户页「检查更新」手动入口。
 *
 * 游戏客户端路径是**用户级**配置（`Switch.GamePath`，留空跟随 BetterGI 全局），
 * 所以入口挂在用户页：目标用户即当前正在编辑的用户，提交 `Update` 模式任务时
 * 直接把它的 uid 当任务 ID，由后端落到该用户的客户端。
 *
 * Args:
 *   getUserId: 当前编辑用户的 uid 取值器。新建用户时 uid 是保存后才补上的，
 *     所以这里取函数而不是快照值。
 */
export function useBetterGIUpdate(getUserId: () => string) {
  const { t } = useI18n()
  const logger = window.electronAPI.getLogger('BetterGI用户更新')
  const { subscribe, unsubscribe } = useWebSocket()

  const updateModal = reactive({
    open: false,
    running: false,
    starting: false,
    log: '',
  })

  const updateSession = reactive({
    subscriptionIds: [] as string[],
    taskId: '',
    timeout: null as number | null,
  })

  // 任务日志增量协议：append 为假 → 整体替换并记 seq；append 为真且 seq 连续 → 追加；
  // 否则视为失步（订阅登记前已经错过首条整体替换、或漏了消息）：丢弃本条，拉一次运行
  // 快照用它的 log/logSeq 重建，重建期间到达的增量一并丢弃。
  let logSeq: number | null = null
  let logResyncing = false
  // 报错后任务随即也会走完成事件，用它抑制紧随其后的成功提示；用户主动取消或
  // 超时收尾时同样置位——那之后到达的完成事件不该再被报成「更新成功」
  let suppressResultToast = false

  const clearSession = () => {
    for (const subscriptionId of updateSession.subscriptionIds) {
      unsubscribe(subscriptionId)
    }
    updateSession.subscriptionIds = []
    updateSession.taskId = ''
    if (updateSession.timeout) {
      window.clearTimeout(updateSession.timeout)
      updateSession.timeout = null
    }
  }

  const stopSession = async (notifyFailure = true): Promise<boolean> => {
    const taskId = updateSession.taskId
    if (!taskId) {
      clearSession()
      return true
    }
    try {
      const response = await Service.stopTaskApiDispatchStopPost({ taskId })
      if (response.code !== 200) {
        throw new Error(response.message || t('edit.bettergiUpdateStopFailed'))
      }
      return true
    } catch (e) {
      logger.error(e instanceof Error ? e.message : String(e))
      if (notifyFailure) {
        // 停止没成功就意味着任务仍在后台跑，不能只写日志让用户以为已经停了
        message.error(t('edit.bettergiUpdateStopFailedRunning'))
      }
      return false
    } finally {
      clearSession()
    }
  }

  /** 任务是否仍在运行；查不到时按仍在运行处理，让超时提示照常出现 */
  const updateStillRunning = async (): Promise<boolean> => {
    if (!updateSession.taskId) return false
    try {
      const snapshot = await realtimeSnapshotApi.getRuntimeTasks()
      return (snapshot.tasks ?? []).some(task => task.taskId === updateSession.taskId)
    } catch (e) {
      logger.warn(`核对原神更新任务状态失败: ${e instanceof Error ? e.message : String(e)}`)
      return true
    }
  }

  const resyncLog = async () => {
    if (logResyncing || !updateSession.taskId) return
    logResyncing = true
    try {
      const snapshot = await realtimeSnapshotApi.getRuntimeTasks()
      const item = (snapshot.tasks ?? []).find(task => task.taskId === updateSession.taskId)
      if (!item) return
      updateModal.log = item.log ?? ''
      logSeq = item.logSeq ?? null
    } catch (e) {
      logger.warn(`重建原神更新日志失败: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      logResyncing = false
    }
  }

  const applyLog = (data: { log: string; seq?: number; append?: boolean }) => {
    if (!data.append) {
      updateModal.log = data.log
      logSeq = data.seq ?? null
    } else if (logSeq !== null && data.seq === logSeq + 1) {
      updateModal.log += data.log
      logSeq = data.seq
    } else {
      logSeq = null
      void resyncLog()
      return
    }
    if (updateModal.log.length > UPDATE_LOG_MAX_CHARS) {
      updateModal.log = updateModal.log.slice(-UPDATE_LOG_MAX_CHARS)
    }
  }

  const handleCheckUpdate = () => {
    if (updateModal.running || !getUserId()) return
    updateModal.log = ''
    logSeq = null
    updateModal.open = true
  }

  const startUpdate = async () => {
    const userId = getUserId()
    if (!userId) return
    updateModal.starting = true
    try {
      const response = await Service.addTaskApiDispatchStartPost({
        taskId: userId,
        mode: TaskCreateIn.mode.UPDATE,
      })
      if (response.code !== 200 || !response.taskId) {
        throw new Error(response.message || t('edit.bettergiUpdateStartFailed'))
      }
      updateModal.running = true
      updateSession.taskId = response.taskId
      if (!updateModal.open) {
        // 请求在途时用户已关掉弹窗：任务已启动但无处展示进度，直接把它停掉
        updateModal.running = false
        await stopSession()
        return
      }
      suppressResultToast = false
      updateSession.subscriptionIds = [
        subscribe({ id: response.taskId, type: WS_TASK_LOG_UPDATED }, wsMessage => {
          applyLog(
            wsMessage.data as unknown as WSTaskLogUpdatedData & { seq?: number; append?: boolean }
          )
        }),
        subscribe({ id: response.taskId, type: WS_TASK_NOTICE }, wsMessage => {
          const data = wsMessage.data as unknown as WSTaskNoticeData
          if (data.level === 'error') {
            suppressResultToast = true
            message.error(t('edit.bettergiUpdateFailed', { p0: data.message }))
            updateModal.running = false
            updateModal.open = false
            void stopSession()
          }
        }),
        subscribe({ id: response.taskId, type: WS_TASK_COMPLETED }, wsMessage => {
          const data = wsMessage.data as unknown as WSTaskCompletedData
          // 用后端给出的 outcome 判定，比只看日志可靠：取消/失败不该报成成功。
          // 任务已结束，无需再发停止请求（那只会白白产生一条失败记录）
          if (!suppressResultToast && data.outcome === 'success') {
            // 已是最新时后端不做任何下载，日志以「无需更新」收尾；此时提示无需
            // 更新，而不是成功样式的「任务已结束」（会被读成更新成功）
            if (updateModal.log.includes('无需更新')) {
              message.info(t('edit.bettergiUpdateUpToDate'))
            } else {
              message.success(t('edit.bettergiUpdateTask'))
            }
          }
          updateModal.running = false
          updateModal.open = false
          clearSession()
        }),
      ]
      updateSession.timeout = window.setTimeout(() => {
        void (async () => {
          // 完成事件已经收过尾（会话已清），超时回调不必再动
          if (!updateSession.taskId) return
          // 先复位状态并抑制提示：对账期间到达的完成事件不该再报一次
          suppressResultToast = true
          updateModal.running = false
          updateModal.open = false
          // 超时前先对账：完成事件在断线或任务秒回时可能丢失，任务其实早已结束，
          // 这时按结束收尾，而不是报一个假的超时
          if (await updateStillRunning()) {
            message.error(t('edit.bettergiUpdateTimed'))
            void stopSession()
          } else {
            message.info(t('edit.bettergiUpdateTask'))
            clearSession()
          }
        })()
      }, UPDATE_TIMEOUT_MS)
    } catch (e) {
      logger.error(e instanceof Error ? e.message : String(e))
      message.error(e instanceof Error ? e.message : t('edit.bettergiUpdateStartFailed'))
    } finally {
      updateModal.starting = false
    }
  }

  const handleUpdateModalCancel = () => {
    if (updateModal.running) {
      // 用户主动取消：随后到达的完成事件不该再报成成功
      suppressResultToast = true
      void stopSession()
    }
    updateModal.running = false
    updateModal.open = false
  }

  onUnmounted(() => {
    // 页面卸载时不再弹提示，失败只记日志
    void stopSession(false)
  })

  return { updateModal, handleCheckUpdate, startUpdate, handleUpdateModalCancel }
}