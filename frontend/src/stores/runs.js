import { defineStore } from 'pinia'
import api from '@/api/axios'

const HISTORY_KEY = 'finsync_run_history'
const POLL_INTERVAL_MS = 2000
const POLL_TIMEOUT_MS = 5 * 60 * 1000 // give up politely after 5 minutes

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []
  } catch {
    return []
  }
}

function saveHistory(history) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, 25)))
}

export const useRunsStore = defineStore('runs', {
  state: () => ({
    history: loadHistory(), // [{ run_id, started_at, ledger_name, bank_name }]
    activeRunId: null,
    status: 'idle', // idle | uploading | PENDING | STARTED | RETRY | SUCCESS | FAILURE
    result: null,
    error: null,
    _pollHandle: null,
  }),
  actions: {
    addToHistory(entry) {
      this.history = [entry, ...this.history.filter((h) => h.run_id !== entry.run_id)]
      saveHistory(this.history)
    },

    reset() {
      this._stopPolling()
      this.activeRunId = null
      this.status = 'idle'
      this.result = null
      this.error = null
    },

    async startReconciliation({ ledgerFile, bankFile }) {
      this.reset()
      this.status = 'uploading'
      this.error = null

      const form = new FormData()
      form.append('ledger_file', ledgerFile)
      form.append('bank_file', bankFile)

      const { data } = await api.post('/api/run_reconciliation', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })

      this.activeRunId = data.run_id
      this.status = 'PENDING'
      this.addToHistory({
        run_id: data.run_id,
        started_at: new Date().toISOString(),
        ledger_name: ledgerFile.name,
        bank_name: bankFile.name,
      })

      this._startPolling(data.run_id)
      return data
    },

    _startPolling(runId) {
      this._stopPolling()
      const startedAt = Date.now()

      const poll = async () => {
        try {
          const { data } = await api.get(`/api/run_status/${runId}`)
          this.status = data.state

          if (data.state === 'SUCCESS') {
            this.result = data.result
            this._stopPolling()
            return
          }
          if (data.state === 'FAILURE') {
            this.error = data.error || 'Reconciliation failed.'
            this._stopPolling()
            return
          }
          if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
            this.error = 'This run is taking longer than expected. Check back from Run History shortly.'
            this._stopPolling()
            return
          }
          this._pollHandle = setTimeout(poll, POLL_INTERVAL_MS)
        } catch (err) {
          this.error = err?.response?.data?.error || 'Lost connection while checking run status.'
          this._stopPolling()
        }
      }

      poll()
    },

    _stopPolling() {
      if (this._pollHandle) {
        clearTimeout(this._pollHandle)
        this._pollHandle = null
      }
    },

    async checkRun(runId) {
      const { data } = await api.get(`/api/run_status/${runId}`)
      return data
    },

    downloadReportUrl(runId) {
      const base = api.defaults.baseURL || ''
      return `${base}/api/download_report/run/${runId}`
    },
  },
})
