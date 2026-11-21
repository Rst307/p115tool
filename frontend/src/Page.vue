<script setup>
import { ref,computed,onMounted,onUnmounted } from 'vue'
import { post,requestErrorMessage } from './host'
import { configurationSection } from './navigation'
import './ui.css'
import Storage from './Storage.vue'
const props=defineProps({api:Object})
const emit=defineEmits(['close','switch','action'])
const tab=ref('organize'),schedule=ref({scheduled:false,time:'02:00'}),target=ref(null)
const enabled=ref(false),error=ref(''),status=ref(null),busy=ref(false)
const sources=ref([]),selected=ref([]),organizeBusy=ref(false),organizeUnknown=ref(false),notice=ref('')
const labels={IDLE:'尚未生成',RUNNING:'正在生成',DONE:'生成完成',PARTIAL:'部分文件失败',FAILED:'扫描失败',INTERRUPTED:'上次任务已中断，可重新生成'}
const errors={SCAN_FAILED:'115目录扫描失败，请检查115连接和目录ID。已生成文件保留，修正配置后可重新生成。',OUTPUT_CONFLICT:'部分文件身份或输出路径存在冲突，请检查已有STRM与源文件。',FILE_GENERATION_FAILED:'部分文件生成失败，请检查输出目录权限和挂载。'}
const refreshing=ref(false), loaded=ref(false)
const running=computed(()=>status.value?.state==='RUNNING')
const stateColor=computed(()=>({DONE:'success',PARTIAL:'warning',FAILED:'error',INTERRUPTED:'warning',RUNNING:'primary'}[status.value?.state] || 'secondary'))
const metrics=computed(()=>[{label:'发现影视文件',value:status.value?.found ?? 0},{label:'生成成功',value:status.value?.generated ?? 0},{label:'生成失败',value:status.value?.failed ?? 0},{label:'已索引文件',value:status.value?.count ?? 0}])
function configure() { configurationSection.value=enabled.value?tab.value:'connection';emit('switch') }
let timer
async function refresh() {
  if(refreshing.value)return
  refreshing.value=true
  try {
    const boot=await post(props.api,'data',{kind:'bootstrap',ui_contract:3});enabled.value=boot.enabled;error.value=boot.error || ''
    if(enabled.value) {
      status.value=await post(props.api,'data',{kind:'status'})
      const organizeData=await post(props.api,'data',{kind:'organize_sources'})
      const items=organizeData.items
      target.value=organizeData.target
      schedule.value={scheduled:organizeData.scheduled,time:organizeData.time}
      if(organizeData.paused)organizeUnknown.value=true
      const first=sources.value.length===0
      sources.value=items.map(item=>({...item,title:`${item.prefix}（${item.cid}）`}))
      selected.value=first?items.map(item=>item.cid):selected.value.filter(cid=>items.some(item=>item.cid===cid))
    }
  } catch(e) { error.value=requestErrorMessage(e,'读取插件状态失败，请尝试刷新状态。') }
  finally {refreshing.value=false;loaded.value=true}
}
async function organize() {
  if(organizeBusy.value || organizeUnknown.value || !selected.value.length || running.value || busy.value)return
  organizeBusy.value=true;error.value='';notice.value=''
  try {
    const result=await post(props.api,'action',{action:'organize',cids:[...selected.value]})
    const messages={TARGET_REQUIRED:'请在整理设置中选择STRM媒体库目标；仅一个媒体库时自动使用。',TARGET_MISMATCH:'目标媒体库ID、路径或类型不一致，请重新选择非根目录并确认宿主115账号。',TARGET_UNAVAILABLE:'MoviePilot无法读取目标媒体库，请检查115存储登录与目录配置。',TARGET_OVERLAP:'待整理目录与目标媒体库重叠，请配置彼此独立的目录。',UNKNOWN:'先前整理提交结果未知，已暂停提交。请核实宿主任务后重新加载插件。',BUSY:'STRM正在生成，请完成后再整理。',UNAVAILABLE:'MoviePilot整理接口不可用，请检查宿主版本和115存储配置。',SOURCE_MISMATCH:'所选目录ID与MoviePilot路径不一致，请在配置页重新选择115文件夹，并确认宿主登录同一115账号。',SOURCE_UNAVAILABLE:'MoviePilot无法读取所选目录，请检查宿主115存储登录状态和目录路径。'}
    if(messages[result.state]) { error.value=messages[result.state];return }
    const count=result.items.filter(item=>item.state==='SUBMITTED').length
    organizeUnknown.value=result.items.some(item=>item.state==='UNKNOWN')
    notice.value=`已向MoviePilot提交 ${count} 个文件夹。整理进度和结果请在MoviePilot整理任务／历史中查看。整理完成后再生成STRM。`
    if(result.state!=='SUBMITTED') error.value=organizeUnknown.value?'整理提交结果未知，已停止后续提交。请在MoviePilot任务／历史核实，不要直接重复点击。':'MoviePilot拒绝了部分整理请求，请在宿主整理任务／历史中检查原因。'
  } catch(e) {
    organizeUnknown.value=true
    error.value=requestErrorMessage(e,'整理提交结果未确认，请先在MoviePilot任务／历史中核实。')
  } finally { organizeBusy.value=false }
}
async function generate() {
  if(busy.value || running.value || organizeBusy.value)return
  busy.value=true;error.value=''
  try { status.value=await post(props.api,'action',{action:'generate'}) }
  catch(e) { error.value=requestErrorMessage(e,'生成任务未启动，请先配置115源文件夹。') }
  finally { busy.value=false }
}
onMounted(()=>{refresh();timer=setInterval(()=>{if(status.value?.state==='RUNNING')refresh()},3000)})
onUnmounted(()=>clearInterval(timer))
</script>
<template>
<section class="p115-ui p115-page">
  <header class="p115-header"><div><div class="p115-eyebrow">115 工具箱 · 媒体库</div><h2>整理、存储与STRM</h2><p>选择功能板块，管理资源存储或生成播放文件。</p></div><div class="p115-actions"><v-btn variant="tonal" prepend-icon="mdi-cog-outline" @click="configure">{{tab==='organize' ? '整理设置' : tab==='storage' ? '存储设置' : 'STRM设置'}}</v-btn><v-btn variant="text" @click="emit('close')">关闭</v-btn></div></header>
  <v-tabs v-model="tab" color="primary" class="mb-4" aria-label="功能板块"><v-tab value="organize">整理</v-tab><v-tab value="strm">STRM</v-tab><v-tab value="storage">存储</v-tab></v-tabs>
  <v-alert v-if="error" type="error" variant="tonal" class="mb-4" role="alert">{{error}}<v-btn v-if="!enabled" variant="text" class="ml-2" :loading="refreshing" @click="refresh">重试读取</v-btn></v-alert>
  <v-alert v-if="notice && tab==='organize'" type="info" variant="tonal" class="mb-4" aria-live="polite">{{notice}}</v-alert>
  <v-progress-linear v-if="!loaded" indeterminate color="primary" aria-label="正在读取插件状态"/>
  <section v-if="loaded && !enabled" class="p115-section p115-onboarding"><v-icon size="40" color="primary">mdi-folder-play-outline</v-icon><h3>先连接你的115媒体库</h3><p>启用插件，选择115连接方式、媒体源目录和输出路径，<br/>再设置Emby可访问的MoviePilot播放地址。</p><v-btn color="primary" class="mt-4" @click="configure">开始配置</v-btn></section>
  <template v-if="enabled">
    <Storage v-if="tab==='storage'" :api="props.api" :generating="running" @configure="configure"/>
    <section v-if="tab==='strm'" class="p115-section p115-generation">
      <div class="p115-section-heading"><div class="p115-feature-icon"><v-icon color="primary" size="28">mdi-play-box-multiple-outline</v-icon></div><div class="p115-grow"><h3>生成STRM</h3><p>扫描已配置的媒体库，保留电影、电视剧及其类别。</p></div><v-chip :color="stateColor" size="small" variant="tonal" aria-live="polite">{{labels[status?.state] || '正在读取状态'}}</v-chip></div>
      <div class="p115-metrics"><div v-for="metric in metrics" :key="metric.label" class="p115-metric"><span>{{metric.label}}</span><strong :class="{'p115-failed':metric.label==='生成失败' && metric.value>0}">{{metric.value}}</strong></div></div>
      <v-progress-linear v-if="running" indeterminate color="primary" class="mt-5" aria-label="STRM正在生成"/>
      <div class="p115-actions mt-5"><v-btn color="primary" prepend-icon="mdi-play" :disabled="!loaded || running || organizeBusy || busy" :loading="busy" @click="generate">{{running ? '正在生成STRM' : '生成STRM'}}</v-btn><v-btn variant="text" prepend-icon="mdi-refresh" :loading="refreshing" :disabled="refreshing" @click="refresh">刷新状态</v-btn></div>
      <p class="p115-note mt-3">{{running ? '任务在后台运行，状态每3秒自动更新，可关闭此页面。' : '首次使用或更改播放地址后，点击生成。STRM输出目录需同时挂载给Emby。'}}</p>
      <v-alert v-if="status?.error" type="warning" variant="tonal" class="mt-4">{{errors[status.error] || '请检查配置后重新生成。'}}</v-alert>
      <p v-if="status?.relocated" class="p115-note mt-3">已修正 {{status.relocated}} 个旧位置STRM，并移除其空目录。</p>
    </section>
    <section v-if="tab==='organize'" class="p115-section">
      <div class="p115-section-heading"><div class="p115-feature-icon"><v-icon color="primary" size="28">mdi-folder-move-outline</v-icon></div><div><h3>交给MoviePilot整理 <span class="p115-count">移动整理</span></h3><p>移动到指定STRM媒体库，成功后原目录不再保留已移动文件。</p></div></div>
      <div class="p115-note mb-4">整理目标：{{target?.prefix || '尚未配置，请在整理设置中选择STRM媒体库'}}。</div>
      <div class="p115-note mb-4">{{schedule.scheduled ? `每天 ${schedule.time}（北京时间）自动整理全部已配置目录` : '定时整理未开启，可在整理设置中开启'}}。下方勾选仅用于本次手动整理。</div>
      <div v-if="!sources.length" class="p115-empty"><v-icon size="28" color="primary">mdi-folder-plus-outline</v-icon><div class="p115-grow"><strong>还没有待整理目录</strong><p>在配置页单独添加待整理文件夹，例如 /下载/待整理。</p></div><v-btn variant="text" color="primary" @click="configure">去配置</v-btn></div>
      <template v-else><v-select v-model="selected" :items="sources" item-title="title" item-value="cid" multiple chips closable-chips label="选择待整理目录" variant="outlined" density="comfortable" :disabled="organizeBusy || running || organizeUnknown" hint="与STRM媒体库独立配置，可一次选择多个目录。" persistent-hint/><div class="p115-actions mt-4"><v-btn variant="tonal" color="primary" prepend-icon="mdi-folder-move-outline" :disabled="!selected.length || running || busy || organizeBusy || organizeUnknown" :loading="organizeBusy" @click="organize">提交整理{{selected.length ? `（${selected.length}个目录）` : ''}}</v-btn><span v-if="running" class="p115-note">STRM生成完成后可提交整理。</span></div></template>
      <p class="p115-note mt-4">请先在MoviePilot配置115存储和媒体库。提交后到MoviePilot查看整理任务与结果，完成后再生成STRM。</p>
      <p class="p115-note mt-3">没有单集编号的电视剧ISO按剧名／Season目录移动，保留原文件名。若宿主已有“成功”但目标路径为空的记录，请仅删除该条整理历史记录、保留源文件，再从这里提交。</p>
      <v-alert v-if="organizeUnknown" type="warning" variant="tonal" class="mt-4">提交结果未确认，已暂停再次提交。请先在MoviePilot任务／历史核实，避免重复整理。</v-alert>
    </section>
    <section v-if="tab==='strm' && status?.paths?.length" class="p115-section"><div class="p115-section-heading"><div><h3>生成路径示例 <span class="p115-count">{{status.paths.length}} 条</span></h3><p>核对媒体分类与输出位置，路径可展开查看。</p></div></div><v-expansion-panels variant="accordion"><v-expansion-panel v-for="item in status.paths" :key="item.output"><v-expansion-panel-title><span class="p115-path-title">{{item.output.split(/[\\/]/).pop()}}</span></v-expansion-panel-title><v-expansion-panel-text><div class="p115-path-row"><span>115源文件</span><code>{{item.source}}</code></div><div class="p115-path-row"><span>STRM输出</span><code>{{item.output}}</code></div></v-expansion-panel-text></v-expansion-panel></v-expansion-panels></section>
  </template>
  <footer class="p115-footer">115 STRM · MoviePilot整理 · 302直链播放</footer>
</section>
</template>
