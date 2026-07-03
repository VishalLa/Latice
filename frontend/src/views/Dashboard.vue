<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useRunsStore } from '@/stores/runs'
import { useToastStore } from '@/stores/toast'
import api from '@/api/axios'
import NavBar from '@/components/NavBar.vue'
import FileUploadDropzone from '@/components/FileUploadDropzone.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import ActionModal from '@/components/ActionModal.vue'

const auth = useAuthStore()
const runs = useRunsStore()
const toast = useToastStore()

const ledgerFile = ref(null)
const bankFile = ref(null)
const ledgerError = ref('')
const bankError = ref('')
const regenModalOpen = ref(false)
const regenTargetRunId = ref(null)
const regenerating = ref(false)

// Rotating "AI is working" copy shown while Celery processes the run.
const processingMessages = [
  'AI is processing matches…',
  'Comparing ledger entries against bank lines…',
  'Scoring fuzzy and memory-based matches…',
  'Almost there — compiling the reconciliation report…',
]
const messageIndex = ref(0)
let messageTimer = null

const isProcessing = computed(() =>
  ['uploading', 'PENDING', 'STARTED', 'RETRY'].includes(runs.status)
)

function startMessageRotation() {
  messageIndex.value = 0
  clearInterval(messageTimer)
  messageTimer = setInterval(() => {
    messageIndex.value = (messageIndex.value + 1) % processingMessages.length
  }, 2600)
}
function stopMessageRotation() {
  clearInterval(messageTimer)
}

onMounted(async () => {
  if (!auth.user) {
    try {
      await auth.fetchCurrentUser()
    } catch {
      // interceptor handles redirect on 401
    }
  }
})

function validateFiles() {
  ledgerError.value = ledgerFile.value ? '' : 'Upload your ledger file to continue.'
  bankError.value = bankFile.value ? '' : 'Upload your bank statement to continue.'
  return !ledgerError.value && !bankError.value
}

async function onRunReconciliation() {
  if (!validateFiles()) return
  try {
    startMessageRotation()
    await runs.startReconciliation({ ledgerFile: ledgerFile.value, bankFile: bankFile.value })
    toast.info('Reconciliation started. This runs in the background.')
    watchForCompletion()
  } catch {
    stopMessageRotation()
    // toast already shown by the axios interceptor
  }
}

function watchForCompletion() {
  const interval = setInterval(() => {
    if (runs.status === 'SUCCESS') {
      stopMessageRotation()
      toast.success('Reconciliation complete.')
      clearInterval(interval)
    } else if (runs.status === 'FAILURE') {
      stopMessageRotation()
      toast.error(runs.error || 'Reconciliation failed.')
      clearInterval(interval)
    }
  }, 500)
}

function resetForm() {
  ledgerFile.value = null
  bankFile.value = null
  ledgerError.value = ''
  bankError.value = ''
  runs.reset()
}

async function downloadReport(runId) {
  try {
    const response = await api.get(`/api/download_report/run/${runId}`, { responseType: 'blob' })
    triggerDownload(response.data, `bank-recon-${runId}.xlsx`)
  } catch (err) {
    if (err?.response?.status === 404) {
      regenTargetRunId.value = runId
      regenModalOpen.value = true
    }
  }
}

function triggerDownload(blob, filename) {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.URL.revokeObjectURL(url)
}

async function confirmRegenerate() {
  if (!regenTargetRunId.value) return
  regenerating.value = true
  try {
    await api.post(`/api/generate_report/run/${regenTargetRunId.value}`, {
      user_id: auth.user?.id,
    })
    toast.info('Report regeneration started. Try downloading again shortly.')
    regenModalOpen.value = false
  } catch {
    // toast already shown by the axios interceptor
  } finally {
    regenerating.value = false
  }
}

const summary = computed(() => runs.result?.summary || {})
</script>

<template>
  <div class="min-h-screen">
    <NavBar />

    <!-- Hero -->
    <section class="relative overflow-hidden bg-hero-dark px-6 py-14">
      <div class="pointer-events-none absolute inset-0 bg-hero-glow"></div>
      <div class="pointer-events-none absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-beam opacity-70"></div>

      <div class="relative z-10 mx-auto max-w-3xl text-center">
        <p class="text-sm font-semibold uppercase tracking-wider text-brand-cyan">First module</p>
        <h1 class="mt-2 text-3xl font-extrabold tracking-tight text-white sm:text-4xl">Smart Reconcile</h1>
        <p class="mt-3 text-sm text-white/60 sm:text-base">
          Upload your general ledger and bank statement — FinSync matches them automatically.
        </p>
      </div>

      <div class="relative z-10 mx-auto mt-10 max-w-3xl rounded-card border border-white/10 bg-ink-800/70 p-6 shadow-glow backdrop-blur sm:p-8">
        <h2 class="text-lg font-bold text-white sm:text-xl">Bank Reconciliation system</h2>

        <div class="mt-6 grid gap-4 sm:grid-cols-2">
          <div class="rounded-card border border-white/10 bg-white/5 p-1">
            <FileUploadDropzone
              v-model="ledgerFile"
              label="Upload ledger file"
              hint="CSV support file, etc."
              :error="ledgerError"
              class="[&_p]:text-white [&_svg]:text-white/60"
            />
          </div>
          <div class="rounded-card border border-white/10 bg-white/5 p-1">
            <FileUploadDropzone
              v-model="bankFile"
              label="Upload bank statement"
              hint="CSV support file, etc."
              :error="bankError"
              class="[&_p]:text-white [&_svg]:text-white/60"
            />
          </div>
        </div>

        <div class="mt-6 flex flex-col items-center gap-3 sm:flex-row sm:justify-between">
          <StatusBadge :status="runs.status" />
          <div class="flex gap-2">
            <button
              v-if="runs.status !== 'idle'"
              type="button"
              class="rounded-pill border border-white/15 px-5 py-2.5 text-sm font-medium text-white/80 transition hover:bg-white/5"
              @click="resetForm"
            >
              Start over
            </button>
            <button
              type="button"
              :disabled="isProcessing"
              class="rounded-pill bg-gradient-to-r from-brand-indigo to-brand-glow px-6 py-2.5 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
              @click="onRunReconciliation"
            >
              {{ isProcessing ? 'Processing…' : 'Run Reconciliation' }}
            </button>
          </div>
        </div>

        <!-- Compelling loading state -->
        <div v-if="isProcessing" class="mt-6 flex items-center gap-3 rounded-lg border border-white/10 bg-white/5 px-4 py-3">
          <span class="relative flex h-2.5 w-2.5">
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-cyan opacity-75"></span>
            <span class="relative inline-flex h-2.5 w-2.5 rounded-full bg-brand-cyan"></span>
          </span>
          <p class="text-sm text-white/80">{{ processingMessages[messageIndex] }}</p>
        </div>

        <p v-if="runs.status === 'FAILURE'" class="mt-4 text-sm font-medium text-rose-400">
          {{ runs.error }}
        </p>
      </div>
    </section>

    <!-- Results -->
    <section v-if="runs.status === 'SUCCESS' && runs.result" class="mx-auto max-w-3xl px-6 py-10">
      <h2 class="text-lg font-bold text-[var(--text-primary)]">Results</h2>
      <div class="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div class="rounded-card border border-[var(--border)] bg-[var(--surface)] p-4">
          <p class="text-xs text-[var(--text-muted)]">Matches found</p>
          <p class="mt-1 text-2xl font-bold text-[var(--text-primary)]">{{ runs.result.matches_found ?? '—' }}</p>
        </div>
        <div class="rounded-card border border-[var(--border)] bg-[var(--surface)] p-4">
          <p class="text-xs text-[var(--text-muted)]">Exact matches</p>
          <p class="mt-1 text-2xl font-bold text-[var(--text-primary)]">{{ summary.exact_matches ?? '—' }}</p>
        </div>
        <div class="rounded-card border border-[var(--border)] bg-[var(--surface)] p-4">
          <p class="text-xs text-[var(--text-muted)]">Fuzzy matches</p>
          <p class="mt-1 text-2xl font-bold text-[var(--text-primary)]">{{ summary.fuzzy_matches ?? '—' }}</p>
        </div>
        <div class="rounded-card border border-[var(--border)] bg-[var(--surface)] p-4">
          <p class="text-xs text-[var(--text-muted)]">AI matches</p>
          <p class="mt-1 text-2xl font-bold text-[var(--text-primary)]">{{ summary.ai_matches ?? '—' }}</p>
        </div>
      </div>

      <button
        v-if="runs.result.download_url"
        type="button"
        class="mt-6 rounded-pill bg-brand-indigo px-6 py-2.5 text-sm font-semibold text-white transition hover:bg-brand-glow"
        @click="downloadReport(runs.activeRunId)"
      >
        Download report (.xlsx)
      </button>
    </section>

    <!-- Run history -->
    <section v-if="runs.history.length" class="mx-auto max-w-3xl px-6 pb-14">
      <h2 class="text-lg font-bold text-[var(--text-primary)]">Run history</h2>
      <ul class="mt-4 divide-y divide-[var(--border)] rounded-card border border-[var(--border)] bg-[var(--surface)]">
        <li v-for="run in runs.history" :key="run.run_id" class="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p class="text-sm font-medium text-[var(--text-primary)]">{{ run.ledger_name }} + {{ run.bank_name }}</p>
            <p class="text-xs text-[var(--text-muted)]">{{ new Date(run.started_at).toLocaleString() }}</p>
          </div>
          <button
            type="button"
            class="self-start rounded-pill border border-[var(--border)] px-4 py-1.5 text-xs font-medium text-[var(--text-primary)] hover:bg-black/5 dark:hover:bg-white/5 sm:self-auto"
            @click="downloadReport(run.run_id)"
          >
            Download report
          </button>
        </li>
      </ul>
    </section>

    <ActionModal
      v-model="regenModalOpen"
      title="Report not found on server"
      confirm-label="Regenerate report"
      :confirm-disabled="regenerating"
      @confirm="confirmRegenerate"
    >
      The original file has been cleared from temporary storage. FinSync can rebuild it from the
      saved reconciliation data instead.
    </ActionModal>
  </div>
</template>
