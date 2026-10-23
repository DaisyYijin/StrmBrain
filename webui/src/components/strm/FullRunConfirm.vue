<script setup lang="ts">
import { computed } from 'vue'
import { TriangleAlert } from '@lucide/vue'
import HAlert from '@/components/hero/HAlert.vue'
import HButton from '@/components/hero/HButton.vue'
import HModal from '@/components/hero/HModal.vue'
import type { FullRunPlan } from '@/pages/strm/useStrmRuns'

/**
 * 「开始全量同步」的确认框（2026-10-23）：把这次扫什么写全再让用户点头。
 * 扫描范围的设置在页签里排得靠后，新用户可能没看到就点了开始 —— 首次全量会按这些后缀把网盘上的文件
 * 全部生成 STRM / 下载到本地；开着失效检测时，后缀比上一次少了，原先同步过的那些文件会被判为失效 STRM
 */
const props = defineProps<{ plan: FullRunPlan | null }>()

const show = computed({
  get: () => !!props.plan,
  set: (v) => {
    if (!v) props.plan?.resolve(false)
  },
})

/** 后缀统一成不带点、小写再比，配置里两种写法都有 */
const norm = (e: string) => e.trim().replace(/^\./, '').toLowerCase()
function dropped(now: string[], before?: string[]) {
  if (!before?.length) return []
  const cur = new Set(now.map(norm))
  return before.map(norm).filter((e) => e && !cur.has(e))
}

const groups = computed(() => {
  const p = props.plan
  if (!p) return []
  return [
    { key: 'video', label: '视频（生成 STRM）', exts: p.cfg.video_ext, gone: dropped(p.cfg.video_ext, p.last?.video_ext) },
    { key: 'image', label: '图片（下载到本地）', exts: p.cfg.image_ext, gone: dropped(p.cfg.image_ext, p.last?.image_ext) },
    {
      key: 'data',
      label: '数据（下载到本地，.nfo 始终包含）',
      exts: p.cfg.data_ext,
      gone: dropped(p.cfg.data_ext, p.last?.data_ext),
    },
  ]
})
const anyGone = computed(() => groups.value.some((g) => g.gone.length))
const firstRun = computed(() => !!props.plan && !props.plan.last)
</script>

<template>
  <HModal v-model:show="show" title="开始全量同步？" width="600px">
    <div v-if="plan" class="body">
      <p class="lead">
        {{
          plan.mode === 'fast'
            ? '用快速模式扫描下面这些库并生成 STRM。快速模式请求少，接口不可用时会自动降级为标准模式。'
            : '用标准模式逐个目录扫描下面这些库并生成 STRM，库大时耗时较长、115 请求较多。'
        }}
      </p>

      <section v-if="plan.libs.length" class="block">
        <h4>扫哪些库（{{ plan.libs.length }} 个）</h4>
        <ul class="libs">
          <li v-for="l in plan.libs" :key="`${l.account}|${l.path}`">
            <b>{{ l.name }}</b>
            <span class="mono">{{ l.path }}</span>
            <span v-if="l.account" class="dim">{{ l.account }}</span>
          </li>
        </ul>
      </section>

      <section class="block">
        <h4>扫描范围</h4>
        <dl class="exts">
          <template v-for="g in groups" :key="g.key">
            <dt>{{ g.label }}</dt>
            <dd>
              <span class="mono">{{ g.exts.length ? g.exts.join(' ') : '无' }}</span>
              <span v-if="g.gone.length" class="gone">
                <TriangleAlert :size="13" />比上次全量少了 {{ g.gone.join(' ') }}
              </span>
            </dd>
          </template>
        </dl>
      </section>

      <section class="block">
        <h4>同步完之后</h4>
        <ul class="after">
          <li>
            失效 STRM 检测：<b>{{ plan.cfg.detect_orphans ? '开' : '关' }}</b>
            <span class="dim">{{ plan.cfg.detect_orphans ? '（只标记网盘上已删的，不删本地，清理要你确认）' : '' }}</span>
          </li>
          <li>
            结束后刷新 Emby：<b>{{ plan.cfg.refresh_emby ? '开' : '关' }}</b>
            <span v-if="plan.cfg.refresh_emby" class="dim">（整个媒体库根重扫，万级库很慢）</span>
          </li>
        </ul>
      </section>

      <HAlert v-if="anyGone && plan.cfg.detect_orphans" status="warning" class="note">
        后缀比上次全量少了：原先按这些后缀同步过的文件，这次会被标成失效 STRM。不是有意缩减的话，先取消，到「扫描范围」里加回来。
      </HAlert>
      <HAlert v-else-if="anyGone" status="default" class="note">
        后缀比上次全量少了：这次不再处理这些文件，已经生成的本地文件不受影响。
      </HAlert>
      <HAlert v-if="firstRun" status="default" class="note">
        第一次全量：网盘上符合这些后缀的文件会全部生成 STRM 或下载到本地。范围不对的话先取消，到全量同步页签的「扫描范围」里改好再来。
      </HAlert>
    </div>
    <template #footer>
      <HButton variant="tertiary" @click="plan?.resolve(false)">取消</HButton>
      <HButton variant="primary" @click="plan?.resolve(true)">开始</HButton>
    </template>
  </HModal>
</template>

<style scoped>
.body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  font-size: 13px;
}
.lead {
  margin: 0;
  line-height: 1.6;
  color: var(--muted);
}
.block h4 {
  margin: 0 0 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--foreground);
}
.libs,
.after {
  list-style: none;
  margin: 0;
  padding: 8px 12px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
  line-height: 1.9;
}
.libs li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 10px;
  min-width: 0;
}
.mono {
  font-family: var(--font-mono);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.dim {
  font-size: 12px;
  color: var(--muted);
}
.exts {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 6px 12px;
  margin: 0;
  padding: 8px 12px;
  border-radius: var(--r-lg);
  background: var(--surface-secondary);
}
.exts dt {
  font-size: 12px;
  color: var(--muted);
}
.exts dd {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gone {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--warning-soft-foreground);
}
.note {
  margin: 0;
}
@media (max-width: 720px) {
  .exts {
    grid-template-columns: 1fr;
  }
}
</style>
