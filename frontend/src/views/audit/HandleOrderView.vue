<template>
  <section class="page handle-page" data-testid="handle-page" :data-dispatch-id="dispatchId">
    <header class="page-head">
      <div>
        <h2>处置接单页 · {{ entry?.['派办编号'] || dispatchId }}</h2>
        <p class="page-desc">
          审计号 {{ entry?.['审计编号'] }}｜现任处置人
          <b data-testid="current-handler">{{ entry?.['处置人'] }}</b>
          <template v-if="entry?.['原处置人']">（原处置人：<s>{{ entry['原处置人'] }}</s>）</template>
          ｜复核人 {{ entry?.['复核人'] }}
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/audit">返回派办中心</RouterLink>
        <RouterLink class="btn" :data-testid="`goto-review-${dispatchId}`"
          :to="`/audit/review/${dispatchId}?as=丙`">打开复核页（丙）</RouterLink>
      </div>
    </header>

    <div class="identity-bar">
      <label>当前提交身份
        <input data-testid="handle-operator" v-model="operatorInput"
               aria-label="处置操作人" @input="onOperatorInput" @change="applyOperator" />
      </label>
      <span class="addr-hint" data-testid="address-hint">
        接单地址：<code>{{ addressLabel }}</code>
      </span>
      <span class="viewer-hint" data-testid="viewer-state">{{ viewerText }}</span>
    </div>

    <div v-if="errorBanner" class="banner reject" data-testid="reject-banner" role="alert">
      <b>请求被拒绝：</b>{{ errorBanner }}
      <span v-if="errorCode" class="code-tag" data-testid="error-code">{{ errorCode }}</span>
    </div>
    <div v-if="successBanner" class="banner success" data-testid="success-banner" role="status">
      {{ successBanner }}
    </div>

    <div v-if="!entry" class="card">加载中或派办单不存在……</div>

    <template v-else>
      <!-- 承继裁决 -->
      <article class="card succession" :data-kind="decision?.kind" data-testid="succession-banner">
        <h3>接班资格裁决：{{ decision?.kind || '暂无' }}</h3>
        <p>{{ decision?.reason || '主管尚未改派，甲按正常流程处置并送审。' }}</p>
        <p class="next-step">
          接单页允许的实际下一步：
          <b data-testid="next-action-label">{{ viewer?.next_action_label || decision?.next_action_label }}</b>
        </p>
        <div class="btn-row">
          <button class="btn" data-testid="accept-handle" type="button" @click="acceptOrder">
            接单（处置 + 重跑任务待办）
          </button>
          <button v-if="viewer?.can_confirm_inherit" class="btn" data-testid="confirm-inherit"
                  type="button" @click="inherit(false)">
            确认承继甲的意见
          </button>
          <button v-if="viewer?.can_confirm_and_submit" class="btn primary"
                  data-testid="confirm-inherit-submit" type="button" @click="inherit(true)">
            确认承继并送审
          </button>
          <button v-if="decision?.direct_inherit" class="btn primary"
                  data-testid="direct-inherit" type="button" @click="inherit(false)">
            直接承继在审意见（无需重复送审）
          </button>
        </div>
      </article>

      <div class="two-col">
        <article class="card">
          <h3>处置意见</h3>
          <textarea
            ref="opinionEl"
            v-model="opinion"
            class="opinion-input"
            data-testid="opinion-input"
            rows="7"
            placeholder="填写客户维度回填异常的核查结论与处置意见……"
          ></textarea>
          <div class="btn-row">
            <button class="btn" data-testid="save-opinion" type="button" @click="saveOpinion">
              暂存意见（仅到服务端，不送丙）
            </button>
            <button class="btn primary" data-testid="submit-review" type="button" @click="submitReview">
              提交送审（送给丙）
            </button>
          </div>
          <p class="hint">
            改派后甲若停留在旧地址再次点“暂存/送审”，请求会到达服务端但被受理边界拒绝，
            并单独写入“请求留痕”；已暂存意见不会被覆盖。
          </p>

          <div class="opinion-panel" data-testid="opinion-panel">
            <h4>服务端现存意见</h4>
            <p v-if="entry.opinion">
              <b>{{ entry.opinion_author }}</b> 于 {{ entry.opinion_saved_at }} 暂存：
            </p>
            <blockquote v-if="entry.opinion" data-testid="existing-opinion">{{ entry.opinion }}</blockquote>
            <p v-else class="empty-state">服务端还没有意见。</p>
            <p v-if="entry.review_status === '已送审'" class="ok-text" data-testid="submitted-tag">
              已送审 · {{ entry.submitted_at }} · 唯一复核材料 #{{ entry.review_material_id }}
            </p>
          </div>
        </article>

        <div>
          <article class="card">
            <h3>A-17 异常与跨模块重跑任务</h3>
            <p><b>事项：</b>{{ entry['事项'] }}</p>
            <p>{{ entry['异常说明'] }}</p>
            <p data-testid="reprocess-link">
              重跑任务：<RouterLink :to="`/reprocess_task?kw=${entry['重跑编号']}`">
                {{ entry['重跑编号'] }}
              </RouterLink>
              （跨模块接单待办已生成给 {{ entry['处置人'] }}）
            </p>
            <p>状态：{{ entry.status }}｜送审：{{ entry.review_status }}｜version {{ entry.version }}</p>
          </article>

          <article class="card">
            <h3>本单请求留痕（受理 / 拒绝分别可查）</h3>
            <table class="data-table" data-testid="request-log-table">
              <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>结果</th><th>code</th></tr></thead>
              <tbody>
                <tr v-for="l in logs" :key="l.id" :data-testid="`log-row-${l.id}`"
                    :data-accepted="String(l.accepted)">
                  <td>{{ l['时间'] }}</td><td>{{ l['操作人'] }}</td><td>{{ l['动作'] }}</td>
                  <td :class="l.accepted ? 'ok-text' : 'error-text'">{{ l['结果'] }}</td>
                  <td>{{ l.code }}</td>
                </tr>
                <tr v-if="!logs.length"><td colspan="5" class="empty-state">暂无记录</td></tr>
              </tbody>
            </table>
          </article>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { auditApi, type AnyRow } from '@/api/audit'
import { initOperator, operator, pickQueryIdentity, setOperator } from './operator'

const props = defineProps<{ id?: string }>()
const route = useRoute()
const dispatchId = computed(() => String(props.id ?? route.params.id ?? ''))

const entry = ref<AnyRow | null>(null)
const logs = ref<AnyRow[]>([])
const opinion = ref('')
const operatorInput = ref(operator.value)
const successBanner = ref('')
const errorBanner = ref('')
const errorCode = ref('')
// 旧地址的关键：令牌只在首次进入页面时从 URL 固定，改派后不会被服务端新令牌替换。
function pickToken(): string {
  for (const key of ['token', 'address_token', 'assignment_token', 'addr']) {
    const v = route.query[key]
    if (typeof v === 'string' && v) return v
  }
  return ''
}
const addressToken = ref(pickToken())

const viewer = computed<AnyRow | null>(() => entry.value?.viewer || null)
const decision = computed<AnyRow | null>(() => entry.value?.succession_decision || null)
const addressLabel = computed(() =>
  `/audit/handle/${dispatchId.value}?as=${operatorInput.value}${addressToken.value ? `&token=${addressToken.value.slice(0, 10)}…` : ''}`)
const viewerText = computed(() => {
  const v = viewer.value
  if (!v?.identity) return ''
  const tags: string[] = []
  if (v.is_current_handler) tags.push('现任处置人')
  if (v.is_former_handler) tags.push('已改派的原处置人（旧资格）')
  if (v.is_supervisor) tags.push('主管')
  return `当前资格：${v.identity}${tags.length ? ` · ${tags.join(' / ')}` : ' · 非本单处置人'}`
})

function onOperatorInput(event: Event) {
  setOperator((event.target as HTMLInputElement).value)
}

function applyOperator() {
  setOperator(operatorInput.value)
  void reload()
}

function showError(result: AnyRow) {
  errorBanner.value = result.message || '操作被拒绝'
  errorCode.value = result.code || ''
  successBanner.value = ''
}
function showSuccess(message: string) {
  successBanner.value = message
  errorBanner.value = ''
  errorCode.value = ''
}

async function reload() {
  const data = await auditApi.detail(dispatchId.value, operator.value, addressToken.value)
  if (data.entry) {
    entry.value = data.entry
    if (!opinion.value && data.entry.opinion) opinion.value = data.entry.opinion
  }
  const logData = await auditApi.logs(dispatchId.value)
  logs.value = (logData.logs || []).slice().reverse()
}

async function acceptOrder() {
  const r = await auditApi.accept(dispatchId.value, operator.value, addressToken.value)
  r.ok ? showSuccess(r.message) : showError(r)
  await reload()
}

async function saveOpinion() {
  const r = await auditApi.saveOpinion(dispatchId.value, opinion.value, operator.value, addressToken.value)
  r.ok ? showSuccess(r.message) : showError(r)
  await reload()
}

async function submitReview() {
  const r = await auditApi.submit(dispatchId.value, operator.value, addressToken.value,
                                  entry.value?.version)
  r.ok ? showSuccess(r.message) : showError(r)
  await reload()
}

async function inherit(andSubmit: boolean) {
  const r = await auditApi.inherit(dispatchId.value, operator.value, addressToken.value, andSubmit)
  r.ok ? showSuccess(r.message) : showError(r)
  await reload()
}

onMounted(() => {
  const who = initOperator(pickQueryIdentity(route.query as Record<string, unknown>), 'handle')
  operatorInput.value = who
  void reload()
})
</script>

<style scoped>
.identity-bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; background: #fff;
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.identity-bar label { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.identity-bar input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; min-width: 140px; }
.addr-hint, .viewer-hint { font-size: 12px; color: var(--muted); }
.addr-hint code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; }
.banner { border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; }
.banner.reject { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.banner.success { background: #ecfdf3; border: 1px solid #6ce9a6; color: #067647; }
.code-tag { margin-left: 8px; background: #b42318; color: #fff; border-radius: 4px; padding: 1px 8px; font-size: 12px; }
.card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.two-col { display: grid; grid-template-columns: 1.1fr 1fr; gap: 12px; }
.two-col > * { min-width: 0; }
.opinion-input { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 10px;
  font-size: 13px; font-family: inherit; resize: vertical; }
.btn-row { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.hint { font-size: 12px; color: var(--muted); }
.opinion-panel { margin-top: 14px; border-top: 1px dashed var(--border); padding-top: 10px; }
.opinion-panel blockquote { margin: 6px 0; padding: 8px 12px; background: #f8fafc;
  border-left: 3px solid var(--brand); border-radius: 4px; font-size: 13px; white-space: pre-wrap; }
.succession .next-step { font-size: 14px; }
.succession[data-kind='直接承继'] { border-color: #6ce9a6; background: #f6fef9; }
.succession[data-kind='确认后送审'] { border-color: #fdb022; background: #fffaeb; }
.ok-text { color: #067647; }
.error-text { color: #b42318; }
@media (max-width: 1000px) { .two-col { grid-template-columns: 1fr; } }
</style>
