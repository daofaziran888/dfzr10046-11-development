<template>
  <section class="page audit-center" data-testid="audit-center">
    <header class="page-head">
      <div>
        <h2>建模审计 · 审计派办</h2>
        <p class="page-desc">
          把客户维度回填异常（A-17）派给处置人与复核人，跨模块生成重跑任务接单待办；
          支持主管改派、意见送审并发裁决、接班资格判定与旧地址提交拒绝留痕。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink v-if="!embedded" class="btn" to="/model_audit">返回建模审计</RouterLink>
        <button class="btn primary" data-testid="seed-scenario" type="button" @click="seedScenario">
          一键构造 A-17 验收场景
        </button>
      </div>
    </header>

    <!-- 身份条 -->
    <div class="identity-bar" data-testid="identity-bar">
      <label>
        当前身份
        <select
          data-testid="operator-switcher"
          :value="operator"
          aria-label="当前身份"
          @change="onSelectChange"
        >
          <option v-for="name in choices" :key="name" :value="name">{{ name }}</option>
        </select>
      </label>
      <label>
        操作人（可填自定义姓名）
        <input
          data-testid="operator-input"
          :value="operator"
          aria-label="操作人"
          placeholder="甲 / 乙 / 丙 / 主管"
          @input="onInputChange"
        />
      </label>
      <span class="identity-hint">所有待办、接单与权限判定都按当前身份请求服务端。</span>
      <span v-if="message" class="inline-msg" :class="{ error: !messageOk }" data-testid="center-message">
        {{ message }}
      </span>
    </div>

    <div class="audit-grid">
      <!-- 新增派办 -->
      <article class="card" data-testid="dispatch-form-card">
        <h3>新增审计派办</h3>
        <div class="form-grid" data-testid="dispatch-form">
          <label>审计编号
            <input v-model="form.audit_no" data-testid="input-audit-no" placeholder="A-17" />
          </label>
          <label>派办编号（可留空自动生成；允许与他单同号）
            <input v-model="form.dispatch_no" data-testid="input-dispatch-no" placeholder="如 PD-A17-01" />
          </label>
          <label>处置人
            <input v-model="form.handler" data-testid="input-handler" />
          </label>
          <label>复核人
            <input v-model="form.reviewer" data-testid="input-reviewer" />
          </label>
          <label class="full">事项
            <input v-model="form.title" data-testid="input-title" placeholder="客户维度回填异常处置与复核" />
          </label>
        </div>
        <div class="page-actions">
          <button class="btn primary" data-testid="submit-dispatch" type="button" @click="createDispatch">
            确认派办并生成甲/丙接单待办
          </button>
        </div>

        <div v-if="auditA17" class="a17-preview" data-testid="a17-preview">
          <h4>A-17 异常原文（建模审计模块）</h4>
          <p><b>{{ auditA17['条目编号'] }}</b> · {{ auditA17['操作类型'] }} · {{ auditA17['操作对象'] }}</p>
          <p>{{ auditA17['异常说明'] || auditA17['变更后值'] }}</p>
        </div>
      </article>

      <!-- 派办单列表 -->
      <article class="card">
        <h3>派办单（{{ dispatches.length }}）</h3>
        <table class="data-table" data-testid="dispatch-table">
          <thead>
            <tr>
              <th>ID</th><th>派办编号</th><th>审计号</th><th>处置人</th><th>复核人</th>
              <th>状态</th><th>承继裁决</th><th>下一步 / 入口</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in dispatches" :key="d.id" :data-testid="`dispatch-row-${d.id}`">
              <td>{{ d.id }}</td>
              <td>{{ d['派办编号'] }}</td>
              <td>{{ d['审计编号'] }}</td>
              <td>
                <span data-testid="dispatch-handler">{{ d['处置人'] }}</span>
                <s v-if="d['原处置人']" class="former">{{ d['原处置人'] }}</s>
              </td>
              <td>{{ d['复核人'] }}</td>
              <td>{{ d.status }}<br /><small>v{{ d.version }} · {{ d.review_status }}</small></td>
              <td data-testid="dispatch-succession">{{ d.succession || '—' }}</td>
              <td class="row-actions wrap">
                <RouterLink class="link" :data-testid="`open-handle-${d.id}`"
                  :to="`/audit/handle/${d.id}?as=${encodeURIComponent(String(d['处置人']))}`">
                  处置接单页（{{ d['处置人'] }}）
                </RouterLink>
                <RouterLink class="link" :data-testid="`open-review-${d.id}`"
                  :to="`/audit/review/${d.id}?as=${encodeURIComponent(String(d['复核人']))}`">
                  复核接单页（{{ d['复核人'] }}）
                </RouterLink>
                <button class="link" :data-testid="`reassign-${d.id}`" type="button"
                  @click="reassignToYi(d.id)">主管改派给乙</button>
              </td>
            </tr>
            <tr v-if="!dispatches.length"><td colspan="8" class="empty-state">
              暂无派办单，可在左侧新增，或点右上角“一键构造 A-17 验收场景”
            </td></tr>
          </tbody>
        </table>
      </article>
    </div>

    <!-- 甲 / 乙 / 丙 待办归属核查 -->
    <article class="card" data-testid="owner-board">
      <h3>待办归属核查（甲 · 乙 · 丙）</h3>
      <div class="owner-grid">
        <div v-for="owner in ownerColumns" :key="owner.key" class="owner-col"
             :data-testid="`owner-column-${owner.key}`" :data-owner="owner.name">
          <h4>{{ owner.name }} 的待办（{{ todosByOwner[owner.name]?.length || 0 }}）</h4>
          <ul class="todo-list">
            <li v-for="t in todosByOwner[owner.name] || []" :key="t.id"
                :data-testid="`todo-row-${t.id}`" :data-state="t.state">
              <span class="todo-kind">[{{ t.kind }}]</span>
              <span>{{ t.title }}</span>
              <span class="badge" :data-testid="`todo-state-${t.id}`">{{ t.state }}</span>
              <RouterLink v-if="t.role === '处置'" class="link"
                          :data-testid="`todo-open-handle-${t.id}`"
                          :to="t['接单地址'] ? addressToRoute(t['接单地址']) : `/audit/handle/${t.dispatch_id}?as=${owner.name}`">
                去处理
              </RouterLink>
              <RouterLink v-else class="link" :data-testid="`todo-open-review-${t.id}`"
                          :to="`/audit/review/${t.dispatch_id}?as=${owner.name}`">
                去复核
              </RouterLink>
            </li>
            <li v-if="!(todosByOwner[owner.name] || []).length" class="empty-state">暂无待办</li>
          </ul>
        </div>
      </div>
    </article>

    <!-- 全部请求留痕 -->
    <article class="card">
      <h3>请求留痕（含被拒的旧地址/越权/重复送审请求）</h3>
      <table class="data-table" data-testid="logs-table">
        <thead><tr><th>时间</th><th>审计号</th><th>操作人</th><th>动作</th><th>结果</th><th>code</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="l in allLogs" :key="l.id" :data-testid="`log-row-${l.id}`"
              :data-accepted="String(l.accepted)">
            <td>{{ l['时间'] }}</td><td>{{ l['审计编号'] }}</td><td>{{ l['操作人'] }}</td>
            <td>{{ l['动作'] }}</td>
            <td :class="l.accepted ? 'ok-text' : 'error-text'">{{ l['结果'] }}</td>
            <td>{{ l.code }}</td><td>{{ l['原因'] }}</td>
          </tr>
          <tr v-if="!allLogs.length"><td colspan="7" class="empty-state">暂无请求记录</td></tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { auditApi, type AnyRow } from '@/api/audit'
import { OPERATOR_CHOICES, operator, setOperator } from './operator'

defineProps<{ embedded?: boolean }>()

const route = useRoute()
const choices = OPERATOR_CHOICES
const message = ref('')
const messageOk = ref(true)
const dispatches = ref<AnyRow[]>([])
const allLogs = ref<AnyRow[]>([])
const auditRows = ref<AnyRow[]>([])
const todosByOwner = reactive<Record<string, AnyRow[]>>({ 甲: [], 乙: [], 丙: [] })
const ownerColumns = [
  { key: 'jia', name: '甲' },
  { key: 'yi', name: '乙' },
  { key: 'bing', name: '丙' },
]

const form = reactive({
  audit_no: 'A-17',
  dispatch_no: '',
  handler: '甲',
  reviewer: '丙',
  title: '客户维度回填异常处置与复核',
})

const auditA17 = computed(() =>
  auditRows.value.find((r) => r['条目编号'] === 'A-17') || null)

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function onSelectChange(event: Event) {
  setOperator((event.target as HTMLSelectElement).value)
  void loadTodos()
}
function onInputChange(event: Event) {
  setOperator((event.target as HTMLInputElement).value)
  void loadTodos()
}

function addressToRoute(address: string) {
  // 后端给的是 /audit/handle/..?as=..&token=.. 形式，直接作为路由路径
  return address
}

async function loadDispatches() {
  const data = await auditApi.listDispatches()
  dispatches.value = data.items || data.dispatches || []
}

async function loadAuditRows() {
  try {
    const resp = await fetch('/api/model_audit?size=200')
    const data = await resp.json()
    auditRows.value = data.items || []
  } catch {
    auditRows.value = []
  }
}

async function loadTodos() {
  for (const owner of ['甲', '乙', '丙']) {
    const data = await auditApi.todos(owner)
    todosByOwner[owner] = data.todos || data.items || []
  }
}

async function loadLogs() {
  const data = await auditGetLogs()
  allLogs.value = (data.logs || data.items || []).slice().reverse()
}

async function auditGetLogs() {
  const resp = await fetch('/api/audit/request-logs')
  return resp.json()
}

async function createDispatch() {
  const result = await auditApi.create({ ...form }, operator.value || '主管')
  flash(result.message, !!result.ok)
  await Promise.all([loadDispatches(), loadTodos(), loadLogs()])
}

async function reassignToYi(id: number) {
  const result = await auditApi.reassign(id, '乙', operator.value || '主管')
  flash(result.message, !!result.ok)
  await Promise.all([loadDispatches(), loadTodos(), loadLogs()])
}

async function seedScenario() {
  const result = await auditApi.seedScenario(true)
  flash(result.message, !!result.ok)
  await Promise.all([loadDispatches(), loadTodos(), loadLogs()])
}

onMounted(async () => {
  if (typeof route.query.as === 'string') setOperator(route.query.as)
  if (typeof route.query.auditNo === 'string') form.audit_no = route.query.auditNo
  try {
    await Promise.all([loadDispatches(), loadAuditRows(), loadTodos(), loadLogs()])
  } catch (error) {
    flash(error instanceof Error ? error.message : '审计派办数据加载失败', false)
  }
})
</script>

<style scoped>
.identity-bar { display: flex; gap: 14px; align-items: end; flex-wrap: wrap; background: #fff;
  border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; }
.identity-bar label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.identity-bar select, .identity-bar input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; min-width: 180px; }
.identity-hint { font-size: 12px; color: var(--muted); }
.inline-msg { font-size: 13px; color: #067647; }
.inline-msg.error { color: #b42318; }
.audit-grid { display: grid; grid-template-columns: minmax(300px, 380px) 1fr; gap: 12px; margin-bottom: 12px; }
.card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.card h3 { margin: 0 0 10px; font-size: 15px; }
.card h4 { margin: 8px 0 4px; font-size: 13px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form-grid .full { grid-column: 1 / -1; }
.form-grid input { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; color: #1f2937; }
.a17-preview { margin-top: 12px; padding: 10px; background: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; font-size: 12.5px; }
.a17-preview p { margin: 4px 0; }
.form-grid + .page-actions { margin-top: 10px; }
.wrap { flex-wrap: wrap; }
.former { color: #b42318; margin-left: 4px; font-size: 12px; }
.owner-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.owner-col { border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fafbfc; }
.owner-col h4 { margin-top: 0; }
.todo-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.todo-list li { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; font-size: 12.5px;
  background: #fff; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
.todo-kind { color: var(--brand); }
.badge { background: #eef2ff; color: #3730a3; border-radius: 999px; padding: 1px 8px; font-size: 11px; }
.ok-text { color: #067647; }
.error-text { color: #b42318; }
@media (max-width: 1100px) { .audit-grid, .owner-grid { grid-template-columns: 1fr; } }
</style>
