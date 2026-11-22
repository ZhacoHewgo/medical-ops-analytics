<script setup>
import { nextTick, onActivated, onBeforeUnmount, onDeactivated, ref } from 'vue'
import { describeAnswer, formatMetric, isMetricValue, useAnalysisRequest } from '../api/analysis.mjs'
import { useSession } from '../api/session.mjs'
import DashboardLoading from '../components/DashboardLoading.vue'
import TrendChart from '../components/charts/TrendChart.vue'
import MotionNumber from '../components/charts/MotionNumber.vue'
import WorkspaceHeading from '../components/WorkspaceHeading.vue'

defineOptions({ name: 'AskDataView' })

const department = useSession().current?.user.department ?? null
const { busy, error, run } = useAnalysisRequest()
const question = ref('')
const turns = ref([])
const thread = ref(null)
const samples = [
  { category: '门诊', text: '2025年6月门诊人次' },
  { category: '住院', text: '2025年4月至6月每月出院人次' },
  { category: '收入', text: '2025年6月药品费占比' },
]
const labels = {
  'MOPS-OUT-01': '门诊人次', 'MOPS-OUT-02': '门诊人次环比', 'MOPS-INP-01': '出院人次',
  'MOPS-INP-02': '平均住院日', 'MOPS-INP-06': '住院次均费用', 'MOPS-INC-01': '收费明细金额',
  'MOPS-INP-04': '床位使用率', 'MOPS-DRUG-02': '药品费占比',
}
let nextId = 0
let frame
let typingTurn
let active = true
onActivated(() => {
  active = true
  if (typingTurn) reveal(typingTurn)
})
onDeactivated(() => {
  active = false
  cancelAnimationFrame(frame)
})
onBeforeUnmount(() => cancelAnimationFrame(frame))

function readAnswer(data) {
  if (!data.scope || (department && data.scope.department !== department)
      || typeof data.unit !== 'string' || !['number', 'line'].includes(data.chart_type)
      || !Array.isArray(data.points) || !data.points.length || data.points.length > 24
      || data.points.some(row => typeof row.start_date !== 'string' || typeof row.end_date !== 'string'
        || row.value !== null && !isMetricValue(row.value))) throw new Error('Invalid answer')
  const answer = {
    department: data.scope.department, unit: data.unit, label: labels[data.indicator_code] ?? '指标结果',
    points: data.points, trend: data.chart_type === 'line' ? data.points.map(row => ({
      date: row.start_date.slice(0, 7), visits: row.value === null ? null : Number(row.value),
    })) : [],
  }
  return { ...answer, sentence: describeAnswer({
    ...answer, nullReason: data.null_reason,
    unavailable: typeof data.source === 'string' && data.source.startsWith('MOPS-QA-CATALOG-'),
  }) }
}

async function ask(text = question.value) {
  const value = text.trim()
  if (busy.value || !value) return
  turns.value = [...turns.value.slice(-9), { id: nextId++, question: value, answer: null, error: '', pending: false, printed: 0 }]
  if (question.value.trim() === value) question.value = ''
  await answerTurn(turns.value.at(-1))
}

async function scrollToLatest() {
  await nextTick()
  if (active && thread.value) thread.value.scrollTop = thread.value.scrollHeight
}

function followsLatest(turn) {
  const log = thread.value
  return active && log && turn.id === turns.value.at(-1)?.id && log.scrollHeight - log.clientHeight - log.scrollTop < 48
}

function finishTyping() {
  cancelAnimationFrame(frame)
  if (!typingTurn) return
  typingTurn.printed = typingTurn.answer.sentence.length
  typingTurn = null
}

function reveal(turn) {
  typingTurn = turn
  if (!active) return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    finishTyping()
    return
  }
  const started = performance.now() - turn.printed * 20
  const length = turn.answer.sentence.length
  function print(now) {
    const printed = Math.min(length, Math.floor((now - started) / 20) + 1)
    if (printed > turn.printed) {
      const follow = followsLatest(turn)
      turn.printed = printed
      if (follow) void scrollToLatest()
    }
    if (printed < length) frame = requestAnimationFrame(print)
    else typingTurn = null
  }
  frame = requestAnimationFrame(print)
}

async function answerTurn(turn) {
  if (busy.value) return
  finishTyping()
  turn.pending = true
  turn.error = ''
  if (turn.id === turns.value.at(-1)?.id) void scrollToLatest()
  const answer = await run('/api/llm/query', {
    body: { question: turn.question }, read: readAnswer, invalid: '请写明指标和统计期间。',
  })
  const follow = followsLatest(turn)
  turn.answer = answer
  turn.error = error.value
  turn.pending = false
  if (answer) reveal(turn)
  if (follow) void scrollToLatest()
}

function handleEnter(event) {
  if (event.isComposing || event.keyCode === 229 || event.shiftKey) return
  event.preventDefault()
  void ask()
}
</script>

<template>
  <main class="workspace section-page ask-workspace">
    <WorkspaceHeading><h1>智能问数</h1></WorkspaceHeading>
    <section class="panel chat-panel" :class="{ 'chat-panel-empty': !turns.length }">
      <div ref="thread" class="chat-thread" role="log" aria-label="对话记录" :tabindex="turns.length ? 0 : undefined">
        <div v-if="!turns.length" class="chat-welcome">
          <span class="chat-bot-avatar" aria-hidden="true"></span>
          <h2>想查哪个指标？</h2>
        </div>
        <article v-for="turn in turns" :key="turn.id" class="chat-turn">
          <p class="chat-question">{{ turn.question }}</p>
          <div class="chat-reply">
            <span class="chat-bot-avatar" :class="{ 'chat-bot-querying': turn.pending }" aria-hidden="true"></span>
            <DashboardLoading :active="turn.pending" layout="chat" label="正在查询…">
              <div v-if="turn.error" class="chat-answer chat-error">
                <p role="alert">{{ turn.error }}</p>
                <button type="button" class="button secondary" :disabled="busy" @click="answerTurn(turn)">重试</button>
              </div>
              <div v-else-if="turn.answer" class="chat-answer"
                :class="{ 'chat-answer-chart': turn.answer.trend.length && turn.printed >= turn.answer.sentence.length }" aria-label="问数结果">
                <p class="chat-sentence">
                  <span v-if="turn.printed < turn.answer.sentence.length" aria-hidden="true">{{ turn.answer.sentence.slice(0, turn.printed) }}<span class="report-cursor"></span></span>
                  <span :class="{ 'report-visually-hidden': turn.printed < turn.answer.sentence.length }">{{ turn.answer.sentence }}</span>
                </p>
                <div v-if="turn.printed >= turn.answer.sentence.length" class="chat-answer-detail">
                  <TrendChart v-if="turn.answer.trend.some(row => row.visits !== null)"
                    :data="turn.answer.trend" :label="turn.answer.label" :unit="turn.answer.unit" period="月" />
                  <div v-else-if="turn.answer.points[0].value !== null" class="dashboard-metric">
                    <MotionNumber :value="turn.answer.points[0].value" :format="value => formatMetric(value, turn.answer.unit)" />
                  </div>
                  <p class="chat-scope">{{ turn.answer.points[0].start_date }} 至 {{ turn.answer.points.at(-1).end_date }} · {{ turn.answer.department || '全院' }}</p>
                </div>
              </div>
            </DashboardLoading>
          </div>
        </article>
      </div>
      <div class="ask-composer">
        <div class="ask-samples" role="group" aria-label="示例问题">
          <button v-for="sample in samples" :key="sample.text" class="button secondary" type="button" :disabled="busy"
            @click="ask(department ? '本科室' + sample.text : sample.text)">
            <small v-if="!turns.length">{{ sample.category }}</small><span>{{ sample.text }}</span>
          </button>
        </div>
        <form class="ask-form" @submit.prevent="ask()">
          <textarea v-model="question" aria-label="问题" rows="1" maxlength="300" required
            placeholder="输入问题" @keydown.enter="handleEnter"></textarea>
          <button type="submit" class="button primary" :disabled="busy || !question.trim()">发送</button>
        </form>
      </div>
    </section>
  </main>
</template>

<style scoped>
.ask-workspace .panel.chat-panel-empty { background: white; border: 1px solid var(--border); border-radius: 16px; }
.chat-panel-empty { grid-template-rows: auto auto; align-content: center; }
.chat-panel-empty .chat-thread { border: 0; overflow: visible; }
.chat-welcome { min-height: 0; padding: 0; gap: 10px; background: transparent; }
.chat-welcome .chat-bot-avatar { width: 68px; height: 68px; }
.chat-welcome h2 { margin: 0; color: var(--ink); font-size: clamp(1.25rem, 2.8vw, 1.85rem); }
.chat-panel-empty .ask-composer { width: min(100%, 710px); margin-inline: auto; padding-top: 0; }
.chat-panel-empty .ask-samples { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.chat-panel-empty .ask-samples button { display: grid; align-content: center; gap: 3px; min-height: 64px; padding: 8px 10px; text-align: left; }
.ask-samples small { color: var(--muted); font-weight: 500; }
.chat-panel-empty .ask-composer textarea { border-radius: 10px; }
@media (max-width: 620px) {
  .chat-panel-empty .ask-samples { grid-template-columns: minmax(0, 1fr); }
  .chat-panel-empty .ask-samples button { grid-template-columns: 2.5em minmax(0, 1fr); align-items: center; min-height: 43px; }
  .chat-panel-empty { min-height: 405px; }
}
</style>
