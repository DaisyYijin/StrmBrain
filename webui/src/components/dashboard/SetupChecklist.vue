<script setup lang="ts">
/**
 * 上手清单：新用户登录后不知道下一步做什么，这张卡片按顺序列出几步、判定做没做、
 * 点一下跳到能做这一步的页签。判定全在后端（internal/api/setup.go，零 115 请求）。
 *
 * **这里不放任何配置表单**，只放跳转：同一份配置多一个编辑入口就多一处整存整取互相覆盖
 * （incr.cron 吃过一次）。唯一的例外是「一键创建工作目录」，它调的是账号页同一个接口、不带参数。
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'
import { Check, ChevronRight, X } from '@lucide/vue'
import { dashboardApi, organizeApi } from '@/api'
import type { SetupStatus, SetupStep } from '@/api/dashboard'
import HButton from '@/components/hero/HButton.vue'
import { toastError, useFeedback } from '@/composables/useFeedback'

const { message } = useFeedback()
const status = ref<SetupStatus | null>(null)
const creating = ref(false)

async function load() {
  try {
    status.value = await dashboardApi.setupStatus()
  } catch {
    // 清单是锦上添花，读不到就不显示，别在总览上报错
    status.value = null
  }
}
onMounted(load)
defineExpose({ load })

/** 每一步去哪儿做。页签值都是各页 ?tab= 认的，别改 */
const LINKS: Record<SetupStep['key'], { to: RouteLocationRaw; label: string }> = {
  pan115: { to: { name: 'accounts', query: { tab: 'account' } }, label: '去登录' },
  library: { to: { name: 'accounts', query: { tab: 'library' } }, label: '去选择' },
  workspace: { to: { name: 'organize', query: { tab: 'basic' } }, label: '去设置' },
  tmdb: { to: { name: 'settings', query: { tab: 'tmdb' } }, label: '去填写' },
  full: { to: { name: 'sync', query: { tab: 'full' } }, label: '去同步' },
  emby: { to: { name: 'settings', query: { tab: 'emby' } }, label: '去设置' },
  organize: { to: { name: 'media-transfer' }, label: '去找片' },
}

const steps = computed(() => status.value?.steps ?? [])
const doneCount = computed(() => steps.value.filter((s) => s.done).length)
/** 下一步：第一个没做的必做项，必做都做完了再轮到可选项 */
const nextKey = computed(
  () => (steps.value.find((s) => !s.done && !s.optional) ?? steps.value.find((s) => !s.done))?.key,
)
const libraryDone = computed(() => steps.value.some((s) => s.key === 'library' && s.done))
/** 全部做完（含可选）就不再占地方；用户点过「不再显示」也不显示 */
const visible = computed(() => !!status.value && !status.value.hidden && doneCount.value < steps.value.length)

async function createWorkspace() {
  creating.value = true
  try {
    const res = await organizeApi.initWorkspace()
    message.success(res.message)
    await load()
  } catch (e) {
    toastError(e, '创建失败')
  } finally {
    creating.value = false
  }
}

async function hide() {
  try {
    await dashboardApi.setupHide(true)
    if (status.value) status.value.hidden = true
    message.success('已隐藏')
  } catch (e) {
    toastError(e, '操作失败')
  }
}
</script>

<template>
  <section v-if="visible" class="card card--default setup" aria-label="上手清单">
    <header class="setup-head">
      <div class="setup-titles">
        <h2 class="setup-title">上手清单</h2>
        <p class="setup-hint">
          {{ status?.done ? '必做的几步都完成了，下面两项可选' : '按顺序做完这几步就能用了' }} · 已完成
          {{ doneCount }} / {{ steps.length }}
        </p>
      </div>
      <HButton size="sm" variant="ghost" aria-label="不再显示" @click="hide">
        <template #icon><X /></template>
        <span class="hide-text">不再显示</span>
      </HButton>
    </header>
    <div class="setup-bar" aria-hidden="true">
      <span :style="{ width: (doneCount / Math.max(1, steps.length)) * 100 + '%' }" />
    </div>
    <ol class="setup-list">
      <li
        v-for="(s, i) in steps"
        :key="s.key"
        class="step"
        :class="{ 'is-done': s.done, 'is-next': s.key === nextKey }"
      >
        <span class="step-mark">
          <Check v-if="s.done" :size="14" />
          <template v-else>{{ i + 1 }}</template>
        </span>
        <div class="step-body">
          <div class="step-title">
            {{ s.title }}<span v-if="s.optional" class="step-opt">可选</span>
          </div>
          <div class="step-detail">{{ s.detail }}</div>
        </div>
        <div v-if="!s.done" class="step-act">
          <HButton
            v-if="s.key === 'workspace' && libraryDone"
            size="sm"
            :variant="s.key === nextKey ? 'primary' : 'secondary'"
            :loading="creating"
            @click="createWorkspace"
          >
            一键创建
          </HButton>
          <RouterLink v-else :to="LINKS[s.key].to" class="step-link" :class="{ 'is-primary': s.key === nextKey }">
            {{ LINKS[s.key].label }}<ChevronRight :size="15" />
          </RouterLink>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.setup {
  gap: 12px;
  padding: 20px;
}
.setup-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.setup-titles {
  flex: 1;
  min-width: 0;
}
.setup-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--foreground);
}
.setup-hint {
  margin: 2px 0 0;
  font-size: 12.5px;
  color: var(--muted);
}
.setup-bar {
  height: 4px;
  border-radius: 999px;
  background: var(--default);
  overflow: hidden;
}
.setup-bar span {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.3s;
}
.setup-list {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}
.step {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-top: 1px solid var(--separator);
}
.step:first-child {
  border-top: 0;
}
.step-mark {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  background: var(--default);
}
.is-next .step-mark {
  color: var(--accent-foreground);
  background: var(--accent);
}
.is-done .step-mark {
  color: var(--success-foreground);
  background: var(--success);
}
.step-body {
  flex: 1;
  min-width: 0;
}
.step-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--foreground);
}
.is-done .step-title {
  color: var(--muted);
}
.step-opt {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: var(--r-sm);
  font-size: 11px;
  font-weight: 400;
  color: var(--muted);
  background: var(--default);
}
.step-detail {
  margin-top: 2px;
  font-size: 12.5px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.step-act {
  flex-shrink: 0;
}
.step-link {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
}
.step-link:hover,
.step-link.is-primary {
  color: var(--accent);
}
.step-link.is-primary {
  font-weight: 600;
}

@media (max-width: 720px) {
  .setup {
    padding: 16px;
  }
  .hide-text {
    display: none;
  }
}
</style>
