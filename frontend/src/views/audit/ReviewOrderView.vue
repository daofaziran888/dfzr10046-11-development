<template>
  <section class="page review-page" data-testid="review-page" :data-dispatch-id="dispatchId">
    <header class="page-head">
      <div>
        <h2>复核接单页 · {{ dispatchId }}</h2>
        <p class="page-desc">以丙的身份打开复核材料；本页同时兼查甲、乙待办，验证归属与受理边界。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/audit">返回派办中心</RouterLink>
        <RouterLink class="btn" :data-testid="`goto-handle-${dispatchId}`"
          :to="`/audit/handle/${dispatchId}?as=乙`">打开处置页（乙）</RouterLink>
      </div>
    </header>

    <div class="identity-bar">
      <label>复核身份
        <input data-testid="review-operator" v-model="operatorInput"
               aria-label="复核操作人" @input="onOperatorInput" @change="applyOperator" />
      </label>
      <span class="viewer-hint" data-testid="review-viewer-state">
        {{ viewer ? `当前：${viewer.identity}${viewer.is_current_reviewer ? ' · 本单现任复核人' : ' · 非本单复核人'}` : '' }}
      </span>
    </div>

    <div v-if="forbidden" class="banner reject" data-testid="forbidden-banner" role="alert">
      <b>无权打开本单复核材料：</b>{{ forbidden }}
      <span v-if="errorCode" class="code-tag" data-testid="review-error-code">{{ errorCode }}</span>
      <p class="boundary-note">
        受理边界：权限按“这一件派办单的复核人资格/地址”判定；丙手头另一件同号记录，
        以及其他单的令牌，都不授予本单权限。
      </p>
    </div>
    <div v-else-if="notice" class="banner waiting" data-testid="waiting-banner">{{ notice }}</div>
    <div v-if="successBanner" class="banner success" data-testid="review-success-banner">{{ successBanner }}</div>

    <div v-if="material" class="material-wrap" data-testid="review-material">
      <article class="card">
        <h3>复核材料 · {{ material['material_no'] || `MAT-${dispatchId}` }}</h3>
        <div class="mat-grid">
          <div><span class="k">审计编号</span><b data-testid="material-audit-no">{{ material['审计编号'] }}</b></div>
          <div><span class="k">派办编号</span><b>{{ material['派办编号'] }}</b></div>
          <div><span class="k">送审人</span><b>{{ material.submitted_by }}</b></div>
          <div><span class="k">承继方式</span><b data-testid="material-succession">{{ material.succession || '甲本人送审' }}</b></div>
        </div>

        <h4>审计异常原文</h4>
        <div class="source-box" data-testid="material-audit-entry">
          <p><b>{{ auditEntry?.['条目编号'] }}</b> · {{ auditEntry?.['操作类型'] }} · {{ auditEntry?.['操作对象'] }}</p>
          <p>{{ auditEntry?.['异常说明'] }}</p>
          <p>变更：{{ auditEntry?.['变更前值'] }} → {{ auditEntry?.['变更后值'] }}</p>
        </div>

        <h4>处置意见（唯一送审版本）</h4>
        <blockquote class="opinion" data-testid="material-opinion">
          {{ material.opinion }}
          <footer>—— {{ material.opinion_author }}</footer>
        </blockquote>

        <h4>跨模块重跑任务</h4>
        <div class="source-box" data-testid="material-reprocess">
          <p v-if="reprocessTask"><b>{{ reprocessTask['重跑编号'] }}</b>
            ｜{{ reprocessTask['触发原因'] }}｜{{ reprocessTask['影响事实表'] }}
            ｜区间 {{ reprocessTask['重跑区间'] }}｜策略 {{ reprocessTask['覆盖策略'] }}
            ｜接单 {{ reprocessTask['接单人'] }} / {{ reprocessTask['接单状态'] }}</p>
          <p v-else>未关联重跑任务。</p>
        </div>

        <div class="btn-row">
          <button class="btn" data-testid="accept-review" type="button" @click="acceptReview">受理复核</button>
        </div>
        <textarea v-model="comment" class="review-comment" data-testid="review-comment" rows="3"
                  placeholder="复核意见（可选）"></textarea>
        <div class="btn-row">
          <button class="btn primary" data-testid="review-pass" type="button" @click="verdict('复核通过')">
            复核通过
          </button>
          <button class="btn danger" data-testid="review-reject" type="button" @click="verdict('退回处置')">
            退回处置
          </button>
        </div>
      </article>

      <article class="card">
        <h3>流转时间线</h3>
        <ol class="timeline" data-testid="dispatch-timeline">
          <li v-for="(t, i) in entry?.timeline || []" :key="i">
            <span class="t-time">{{ t['时间'] }}</span>
            <b>{{ t['操作人'] }}</b> · {{ t['动作'] }} —— {{ t['说明'] }}
          </li>
        </ol>
      </article>
    </div>

    <!-- 兼查甲、乙、丙待办：归属与受理边界 -->
    <article class="card" data-testid="boundary-board">
      <h3>兼查甲 / 乙 / 丙待办（归属与受理边界）</h3>
      <div class="owner-grid">
        <div v-for="owner in owners" :key="owner.key" class="owner-col"
             :data-testid="`review-owner-column-${owner.key}`" :data-owner="owner.name">
          <h4>{{ owner.name }}（{{ todos[owner.name]?.length || 0 }}）</h4>
          <ul class="todo-list">
            <li v-for="t in todos[owner.name] || []" :key="t.id" :data-testid="`review-todo-row-${t.id}`"
                :data-state="t.state">
              <span class="todo-kind">[{{ t.kind }}]</span>
              <span>{{ t.title }}</span>
              <span class="badge">{{ t.state }}</span>
            </li>
            <li v-if="!(todos[owner.name] || []).length" class="empty-state">暂无待办</li>
          </ul>
        </div>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { auditApi, type AnyRow } from '@/api/audit'
import { initOperator, operator, pickQueryIdentity, setOperator } from './operator'

const props = defineProps<{ id?: string }>()
const route = useRoute()
const dispatchId = computed(() => String(props.id ?? route.params.id ?? ''))

const entry = ref<AnyRow | null>(null)
const material = ref<AnyRow | null>(null)
const auditEntry = ref<AnyRow | null>(null)
const reprocessTask = ref<AnyRow | null>(null)
const forbidden = ref('')
const notice = ref('')
const errorCode = ref('')
const successBanner = ref('')
const comment = ref('')
const operatorInput = ref(operator.value)
function pickToken(): string {
  for (const key of ['token', 'address_token', 'assignment_token', 'addr']) {
    const v = route.query[key]
    if (typeof v === 'string' && v) return v
  }
  return ''
}
const addressToken = ref(pickToken())
const todos = reactive<Record<string, AnyRow[]>>({ 甲: [], 乙: [], 丙: [] })
const owners = [
  { key: 'jia', name: '甲' },
  { key: 'yi', name: '乙' },
  { key: 'bing', name: '丙' },
]

const viewer = computed(() => entry.value?.viewer || null)

function onOperatorInput(event: Event) {
  setOperator((event.target as HTMLInputElement).value)
}

function applyOperator() {
  setOperator(operatorInput.value)
  void loadMaterial()
}

async function loadOwnerTodos() {
  for (const name of ['甲', '乙', '丙']) {
    const data = await auditApi.todos(name)
    todos[name] = data.todos || data.items || []
  }
}

async function loadMaterial() {
  forbidden.value = ''
  notice.value = ''
  material.value = null
  const result = await auditApi.material(dispatchId.value, operator.value || '丙', addressToken.value)
  if (result.entry) entry.value = result.entry
  if (result.ok) {
    material.value = result.entry?.material || result.material || result.entry
    auditEntry.value = result.entry?.audit_entry || null
    reprocessTask.value = result.entry?.reprocess_task || null
  } else if (result.code === 'NOT_SUBMITTED') {
    notice.value = result.message
  } else {
    forbidden.value = result.message
    errorCode.value = result.code || ''
  }
  await loadOwnerTodos()
}

async function acceptReview() {
  const r = await auditApi.accept(dispatchId.value, operator.value || '丙', addressToken.value, '复核')
  r.ok ? (successBanner.value = r.message) : (forbidden.value = r.message)
  await loadMaterial()
}

async function verdict(v: string) {
  const r = await auditApi.verdict(dispatchId.value, v, comment.value,
                                   operator.value || '丙', addressToken.value)
  if (r.ok) {
    successBanner.value = r.message
    forbidden.value = ''
  } else {
    forbidden.value = r.message
    successBanner.value = ''
  }
  await loadMaterial()
}

onMounted(() => {
  const qid = pickQueryIdentity(route.query as Record<string, unknown>)
  const who = initOperator(qid, 'review')
  operatorInput.value = who === '主管' ? '丙' : who
  if (operatorInput.value === '主管') setOperator('丙')
  void loadMaterial()
})
</script>

<style scoped>
.identity-bar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; background: #fff;
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.identity-bar label { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.identity-bar input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; min-width: 140px; }
.viewer-hint { font-size: 12px; color: var(--muted); }
.banner { border-radius: 8px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; }
.banner.reject { background: #fef3f2; border: 1px solid #fda29b; color: #b42318; }
.banner.waiting { background: #fffaeb; border: 1px solid #fedf89; color: #b54708; }
.banner.success { background: #ecfdf3; border: 1px solid #6ce9a6; color: #067647; }
.code-tag { margin-left: 8px; background: #b42318; color: #fff; border-radius: 4px; padding: 1px 8px; font-size: 12px; }
.boundary-note { margin: 8px 0 0; font-size: 12.5px; }
.card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.mat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 8px; }
.mat-grid .k { display: block; font-size: 12px; color: var(--muted); }
.source-box { background: #f8fafc; border-radius: 6px; padding: 8px 12px; font-size: 13px; margin-bottom: 10px; }
.source-box p { margin: 4px 0; }
.opinion { background: #f0f7ff; border-left: 3px solid var(--brand); border-radius: 4px;
  padding: 10px 14px; margin: 6px 0 12px; white-space: pre-wrap; font-size: 14px; }
.opinion footer { margin-top: 6px; font-size: 12px; color: var(--muted); }
.review-comment { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 10px;
  font-family: inherit; font-size: 13px; margin-top: 10px; }
.btn-row { display: flex; gap: 10px; margin: 10px 0; }
.btn.danger { background: #d92d20; border-color: #d92d20; color: #fff; }
.timeline { list-style: none; margin: 0; padding: 0; }
.timeline li { padding: 6px 0; border-bottom: 1px dashed var(--border); font-size: 13px; }
.t-time { color: var(--muted); margin-right: 8px; font-size: 12px; }
.owner-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.owner-col { border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fafbfc; }
.owner-col h4 { margin: 0 0 8px; }
.todo-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.todo-list li { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; font-size: 12.5px;
  background: #fff; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.todo-kind { color: var(--brand); }
.badge { background: #eef2ff; color: #3730a3; border-radius: 999px; padding: 1px 8px; font-size: 11px; }
@media (max-width: 1000px) { .owner-grid, .mat-grid { grid-template-columns: 1fr; } }
</style>
