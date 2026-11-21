<script setup>
import {ref,computed,onMounted,onUnmounted} from 'vue'
import {post,requestErrorMessage} from './host'
import {storageTypes,storageStages,storagePhases,storageErrorMessage,conversionResultMessage,virtualizationPayload,temporaryStorageHint,storageActionMessage,recycleCheckpointMessage,canSubmitShare,recycleEmptyMessage} from './storage'
const props=defineProps({api:Object,generating:Boolean})
const emit=defineEmits(['configure'])
const items=ref([]),total=ref(0),page=ref(1),kind=ref('ALL'),search=ref(''),selected=ref([])
const job=ref({state:'IDLE'}),working=ref(false),loading=ref(false),error=ref(''),notice=ref('')
const days=ref(7),automatic=ref(true),consent=ref(false),virtualDialog=ref(false),importDialog=ref(false)
const allActual=ref(false),queued=ref(false)
const importQueue=ref({pending:0,limit:100,tasks:[]})
const purgeSource=ref(true),recycleKeyConfigured=ref(false)
const deleteDialog=ref(false),deleteIds=ref([])
const tempConfigured=ref(null),purgeEnabled=ref(false),purgeBlocked=ref(false)
const nativeAuth=ref(false)
const purgePending=ref([]),purgePendingTotal=ref(0)
const recycleEmpty=ref({scheduled:false,stage:'IDLE',blocked:false})
const sourcesConfigured=ref(false),scannedAt=ref(null)
const scanTime=computed(()=>scannedAt.value?new Date(scannedAt.value*1000).toLocaleString():'尚未完成扫描')
const link=ref(''),password=ref(''),attachId=ref(''),shareDialog=ref(false),share=ref(null)
const blocked=computed(()=>working.value || queued.value || (job.value.state==='RUNNING' && job.value.action!=='scan') || props.generating)
const shareImportBlocked=computed(()=>!canSubmitShare({working:working.value,blocked:blocked.value,pending:importQueue.value.pending,limit:importQueue.value.limit}))
const conversionProgress=computed(()=>['virtualize','virtualize_all','import'].includes(job.value.action) && !['SCANNING','REGISTERING','STARTING'].includes(job.value.phase) && job.value.total>0 ? Math.min(100,100*((job.value.done || 0)+(job.value.failed || 0))/job.value.total) : null)
const actualSelection=computed(()=>selected.value.filter(id=>items.value.some(x=>x.id===id && x.kind==='ACTUAL')))
let timer
function stage(value){return storageStages[value] || value || '未创建副本'}
async function refresh(){
  if(loading.value)return
  loading.value=true
  try{
    const result=await post(props.api,'data',{kind:'storage',page:page.value,storage:kind.value,search:search.value})
    recycleEmpty.value=await post(props.api,'data',{kind:'recycle_empty'})
    items.value=result.items;total.value=result.total;job.value=result.job;queued.value=result.queued===true;days.value=result.temp_days;automatic.value=result.cleanup_enabled
    importQueue.value=result.import_queue || {pending:0,limit:100,tasks:[]}
    tempConfigured.value=result.temp_configured===true
    nativeAuth.value=result.use_moviepilot_auth===true
    purgeEnabled.value=result.recycle_purge_enabled===true;purgeBlocked.value=result.recycle_purge_blocked===true
    purgePending.value=result.recycle_purge_pending || [];purgePendingTotal.value=result.recycle_purge_pending_total || 0
    recycleKeyConfigured.value=result.recycle_key_configured===true
    sourcesConfigured.value=result.sources_configured===true;scannedAt.value=result.scanned_at
    error.value=''
    selected.value=selected.value.filter(id=>items.value.some(x=>x.id===id))
  }catch(e){error.value=requestErrorMessage(e,'存储状态读取失败，请重试。')}
  finally{loading.value=false}
}
async function act(payload){
  if(payload.action==='import_share' ? shareImportBlocked.value : blocked.value)return
  working.value=true;error.value='';notice.value=''
  try{
    const result=await post(props.api,'action',payload)
    notice.value=storageActionMessage(result)
    await refresh()
    return result
  }catch(e){error.value=requestErrorMessage(e,'提交未确认。请刷新状态或核实网盘结果，勿直接重复提交。')}
  finally{working.value=false}
}
function openImport(id=''){attachId.value=id;link.value='';password.value='';importDialog.value=true}
function openDelete(ids){deleteIds.value=[...ids];deleteDialog.value=true}
async function deleteRecords(){
  const ids=[...deleteIds.value];deleteDialog.value=false;deleteIds.value=[]
  await act({action:'delete_storage_records',ids})
}
async function submitImport(){
  const payload=attachId.value?{action:'attach_share',id:attachId.value,link:link.value,password:password.value}:{action:'import_share',link:link.value,password:password.value}
  const result=await act(payload)
  if(['RUNNING','QUEUED','ALREADY_QUEUED','ATTACHED'].includes(result?.state)){
    importDialog.value=false;link.value='';password.value='';attachId.value=''
  }
}
async function virtualize(){const payload=virtualizationPayload(actualSelection.value,consent.value,allActual.value,purgeSource.value || purgeEnabled.value);virtualDialog.value=false;consent.value=false;await act(payload)}
async function showShare(id){
  share.value=null;error.value=''
  try{share.value=await post(props.api,'data',{kind:'share_details',id});shareDialog.value=true}
  catch(e){error.value=requestErrorMessage(e,'分享信息不可用，请先完成虚拟化或核实检查点。')}
}
function filter(){page.value=1;selected.value=[];refresh()}
onMounted(async()=>{timer=setInterval(()=>refresh(),3000);await refresh()})
onUnmounted(()=>{clearInterval(timer);link.value='';password.value='';share.value=null})
</script>
<template>
<section class="p115-section">
  <div class="p115-section-heading"><div class="p115-grow"><h3>实际存储与虚拟存储</h3><p>STRM地址保持稳定；虚拟资源的临时副本到期清理后可重新转存。</p></div><v-btn variant="text" @click="emit('configure')">存储设置</v-btn></div>
  <p class="p115-note mb-3">实际资源递归扫描“STRM生成源文件夹”（整理后的媒体库），无需先生成STRM。后台每30分钟刷新，115整理成功后合并刷新一次；打开页面、搜索和分页只读本地快照，临时副本目录不计入。上次完整扫描：{{scanTime}}。</p>
  <v-alert v-if="!sourcesConfigured && !loading" type="info" variant="tonal" class="mb-4">请在STRM设置中选择整理后的媒体库源文件夹，再扫描实际资源。</v-alert>
  <div class="p115-actions mb-3"><v-btn variant="tonal" prepend-icon="mdi-folder-search-outline" :disabled="blocked || !sourcesConfigured" @click="act({action:'scan_storage'})">重新扫描媒体库</v-btn></div>
  <v-alert v-if="job.action==='scan' && ['FAILED','INTERRUPTED'].includes(job.state)" type="warning" variant="tonal" class="mb-4">媒体库扫描未完成，保留上次完整扫描结果。请检查115连接和源目录后重新扫描。</v-alert>
  <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{error}}</v-alert>
  <v-alert v-if="notice" type="info" variant="tonal" class="mb-4">{{notice}}</v-alert>
  <v-alert v-if="nativeAuth" type="info" variant="tonal" class="mb-4">当前使用 MoviePilot 原生授权。分享导入、虚拟播放、副本清理和回收站操作需要 Cookie 模式。</v-alert>
  <template v-else>
  <v-alert v-if="tempConfigured===false" type="warning" variant="tonal" class="mb-4">{{temporaryStorageHint}}<div class="mt-2"><v-btn variant="text" @click="emit('configure')">打开存储设置</v-btn></div></v-alert>
  <div class="p115-actions mb-4"><v-btn color="primary" prepend-icon="mdi-link-plus" :disabled="shareImportBlocked" @click="openImport()">添加115分享</v-btn><v-btn variant="tonal" :disabled="blocked || !actualSelection.length" @click="allActual=false;purgeSource=true;consent=false;virtualDialog=true">转为虚拟存储（{{actualSelection.length}}）</v-btn><v-btn variant="tonal" :disabled="blocked || !sourcesConfigured" @click="allActual=true;purgeSource=true;consent=false;virtualDialog=true">全部实际存储转为虚拟</v-btn><v-btn variant="text" :disabled="blocked || !selected.length" @click="act({action:'reconcile_storage',ids:[...selected]})">只读核实结果</v-btn><v-btn variant="text" :disabled="blocked" @click="act({action:'cleanup_copies'})">清理到期副本</v-btn></div>
  <p class="p115-note mb-4">副本保留 {{days}} 天，自动清理{{automatic?'已开启':'未开启'}}。每次播放保护至少24小时。副本删除后按需重转存；转存待恢复时自动核实，旧目录为空或已删除则换新目录恢复，连续失败间隔至少30秒。分享失效时保留副本；库内源视频仅在你选择并确认虚拟化时删除。</p>
  <v-alert v-if="recycleEmpty.scheduled || recycleEmpty.stage!=='IDLE'" :type="recycleEmpty.blocked || ['FAILED','BLOCKED'].includes(recycleEmpty.stage)?'warning':'info'" variant="tonal" class="mb-4" aria-live="polite">
    定时清空整个回收站：{{recycleEmpty.scheduled?`每天 ${recycleEmpty.time}（北京时间）`:'已关闭'}}，永久删除全部回收站条目（包括历史及其他应用删除的文件），无法恢复。
    <div class="mt-2">{{recycleEmptyMessage(recycleEmpty)}} 已核实删除 {{recycleEmpty.done || 0}} / {{recycleEmpty.total || 0}} 个条目。<span v-if="recycleEmpty.updated_at">更新时间：{{new Date(recycleEmpty.updated_at*1000).toLocaleString()}}。</span></div>
    <v-btn v-if="recycleEmpty.blocked" variant="text" :disabled="blocked" @click="act({action:'reconcile_empty_recycle'})">只读核实清空结果</v-btn>
  </v-alert>
  </template>
  <div class="p115-grid"><v-select v-model="kind" :items="[{title:'全部资源',value:'ALL'},{title:'实际存储',value:'ACTUAL'},{title:'虚拟存储',value:'VIRTUAL'}]" label="存储类型" variant="outlined" @update:model-value="filter"/><v-text-field v-model="search" label="搜索名称或路径" variant="outlined" maxlength="100" @keyup.enter="filter"/></div>
  <div class="p115-actions mb-3"><v-btn variant="text" :loading="loading" @click="filter">搜索／刷新</v-btn><span class="p115-note">共 {{total}} 个资源；每页50条</span></div>
  <div class="p115-actions mb-3"><v-btn color="error" variant="tonal" prepend-icon="mdi-delete-outline" :disabled="blocked || !selected.length" @click="openDelete(selected)">删除存储记录（{{selected.length}}）</v-btn></div>
  <p v-if="!nativeAuth && job.state==='RUNNING' && job.action==='scan'" class="p115-note mb-3">刷新期间可选择并提交文件操作；操作将在扫描后执行，并核实文件身份和存在性。</p>
  <v-progress-linear v-if="job.state==='RUNNING'" :indeterminate="conversionProgress===null" :model-value="conversionProgress || 0" color="primary" aria-label="转换处理进度"/>
  <v-alert v-if="!nativeAuth && purgeBlocked" type="warning" variant="tonal" class="mb-4">回收站永久删除有 {{purgePendingTotal}} 个未核实检查点，不会自动重试。<v-btn variant="text" :disabled="blocked" @click="act({action:'reconcile_recycle'})">只读核实回收站检查点</v-btn><p class="mt-2">每次最多核实100条；仅解除已记录目标且目标及源文件均不存在的检查点，目标仍在或身份缺失时保留，不提交删除。</p>开启全局或本次永久删除时暂停后续删除；关闭两处永久删除选项可继续普通删除，不补删旧条目。
    <div v-for="(checkpoint,index) in purgePending" :key="index" class="mt-2">关联文件 {{checkpoint.file_id || 'ID无法确认'}}：{{recycleCheckpointMessage(checkpoint)}}<span v-if="checkpoint.operation!=='unknown'"> 接口：{{checkpoint.operation}}。</span><span v-if="checkpoint.upstream_code!=null"> 115错误码：{{checkpoint.upstream_code}}。</span></div>
    <div v-if="purgePendingTotal>purgePending.length" class="mt-2">仅显示前 {{purgePending.length}} 个检查点。</div>
  </v-alert>
  <v-alert v-else-if="!nativeAuth && purgeEnabled" type="warning" variant="tonal" class="mb-4">已启用回收站永久删除：实际转虚拟和到期临时副本删除后，匹配条目将被永久删除，无法从回收站恢复。</v-alert>
  <v-alert v-if="job.state!=='IDLE'" :type="['FAILED','PARTIAL','INTERRUPTED'].includes(job.state)?'warning':'info'" variant="tonal" class="mb-4" aria-live="polite">任务{{({RUNNING:'运行中',DONE:'完成',PARTIAL:'部分未完成',FAILED:'失败',INTERRUPTED:'已中断'})[job.state] || job.state}}：成功 {{job.done || 0}}，未完成 {{job.failed || 0}}<span v-if="job.total!=null">，共 {{job.total}} 个</span><span v-if="job.remaining">，尚未处理 {{job.remaining}} 个</span>。<span v-if="job.pending">等待批次末尾复查 {{job.pending}} 个。</span><span v-if="job.rechecked">已复查 {{job.rechecked}} 个。</span><div v-if="job.state==='RUNNING'">当前步骤：{{storagePhases[job.phase] || '处理资源'}}<span v-if="job.current">（文件ID：{{job.current}}）</span><span v-if="job.phase==='SCANNING'">，已扫描 {{job.scanned || 0}} 个目录，发现 {{job.found || 0}} 个视频</span><span v-if="job.phase==='REGISTERING'">，已登记 {{job.registered || 0}} / {{job.total || 0}} 个视频</span>。</div><div v-if="job.error">{{storageErrorMessage(job.error)}}</div><div v-for="result in (job.results || []).filter(r=>r.state==='FAILED')" :key="result.id" class="mt-2">文件 {{result.id}}：{{conversionResultMessage(result)}}<span v-if="result.upstream_code!=null">（115错误码：{{result.upstream_code}}）</span></div></v-alert>
  <v-card v-if="importQueue.tasks.length" variant="outlined" class="mb-4" aria-label="分享导入队列">
    <v-card-title>分享导入队列 · 等待 {{importQueue.pending}} / {{importQueue.limit}}</v-card-title>
    <v-card-text>
      <p class="p115-note mb-3">可继续添加分享，按提交顺序处理。单项失败后继续下一项；等待项仅保存在内存中，插件重启后需重新添加。保留最近50项已结束任务。</p>
      <div v-for="task in importQueue.tasks" :key="task.id" class="mb-3" aria-live="polite">
        <strong>分享任务 {{task.number}}</strong> · {{({QUEUED:'等待中',RUNNING:'处理中',DONE:'完成',PARTIAL:'部分未完成',FAILED:'失败',INTERRUPTED:'已中断'})[task.state] || '待核实'}}
        <span v-if="task.state!=='QUEUED'"> · 成功 {{task.done || 0}}，未完成 {{task.failed || 0}}<span v-if="task.total!=null">，共 {{task.total}} 个</span></span>
        <span v-if="task.state==='RUNNING' && task.phase==='SCANNING'"> · 已扫描 {{task.scanned || 0}} 个目录，发现 {{task.found || 0}} 个视频</span>
        <div v-if="task.error">{{storageErrorMessage(task.error)}}</div>
      </div>
    </v-card-text>
  </v-card>
  <div v-if="!items.length && !loading" class="p115-empty">尚无匹配资源。扫描整理后的媒体库或导入115分享。</div>
  <v-list v-else bg-color="transparent" class="p115-folder-list">
    <v-list-item v-for="item in items" :key="item.id" :title="item.name" :subtitle="item.path">
      <template #append><v-btn size="small" color="error" variant="text" :disabled="blocked" @click="openDelete([item.id])">删除记录</v-btn></template>
      <template #prepend><v-checkbox-btn v-model="selected" :value="item.id" :disabled="blocked" :aria-label="`选择 ${item.name}`"/></template>
      <div class="p115-actions mt-2"><v-chip size="small" :color="item.kind==='VIRTUAL'?'primary':'secondary'">{{storageTypes[item.kind]}}</v-chip><span class="p115-note">{{stage(item.stage)}}</span><span v-if="item.kind==='VIRTUAL' || item.copy_state" class="p115-note">临时副本：{{stage(item.copy_state)}}</span><v-btn v-if="!nativeAuth && item.stage==='SHARE_CREATE_UNKNOWN'" size="small" variant="text" :disabled="blocked" @click="openImport(item.id)">接入已创建分享</v-btn><v-btn v-if="!nativeAuth && ['SOURCE_DELETE_UNKNOWN','SHARE_UNAVAILABLE'].includes(item.stage)" size="small" variant="text" :disabled="blocked" @click="act({action:'reconcile_storage',ids:[item.id]})">只读核实此资源</v-btn><v-btn v-if="!nativeAuth && item.kind==='ACTUAL' && ['SHARE_CHECK','SHARE_REJECTED'].includes(item.stage)" size="small" variant="text" :disabled="blocked" @click="selected=[item.id];allActual=false;purgeSource=true;consent=false;virtualDialog=true">继续转换</v-btn><v-btn v-if="item.kind==='VIRTUAL'" size="small" variant="text" @click="showShare(item.id)">查看分享</v-btn></div>
    </v-list-item>
  </v-list>
  <v-pagination v-if="total>50" v-model="page" :length="Math.ceil(total/50)" :total-visible="5" @update:model-value="selected=[];refresh()"/>
  <v-dialog v-model="deleteDialog" max-width="600"><v-card title="删除存储记录"><v-card-text><p>将所选 {{deleteIds.length}} 条记录从存储列表移除，后续扫描和重启不会自动恢复显示，也不会纳入“全部实际存储转为虚拟”。</p><p class="mt-3">不删除网盘源文件、临时副本、分享或STRM，原播放地址保留。已有临时副本继续按原到期规则清理；未核实的操作检查点保留。</p></v-card-text><v-card-actions><v-btn @click="deleteDialog=false;deleteIds=[]">取消</v-btn><v-spacer/><v-btn color="error" :disabled="blocked || !deleteIds.length" @click="deleteRecords">确认删除记录</v-btn></v-card-actions></v-card></v-dialog>
  <v-dialog v-model="importDialog" max-width="600"><v-card :title="attachId?'接入已创建分享':'导入115分享'"><v-card-text><p class="mb-4">{{attachId?'用于核实先前创建结果未知的分享，不重新创建分享。':'自动提取分享内的视频，调用MoviePilot识别、分类和命名模板进行虚拟整理，生成STRM并加入虚拟存储。导入不转存，首次播放才临时转存；播放前请配置临时目录。支持在其他任务运行时继续添加分享，按提交顺序逐个处理；每个分享最多1万个视频。识别失败的文件会计为未完成，可重新导入重试。'}}</p><v-text-field v-model="link" label="115分享链接" variant="outlined" autocomplete="off"/><v-text-field v-model="password" label="提取码（链接含提取码时可留空）" type="password" maxlength="4" variant="outlined" autocomplete="off"/></v-card-text><v-card-actions><v-btn @click="importDialog=false;link='';password=''">取消</v-btn><v-spacer/><v-btn color="primary" :disabled="!link || (attachId ? blocked : shareImportBlocked)" @click="submitImport">{{attachId?'核实并接入':'添加到导入队列'}}</v-btn></v-card-actions></v-card></v-dialog>
  <v-dialog v-model="virtualDialog" max-width="600"><v-card :title="allActual?'全部实际存储转为虚拟':'转为虚拟存储'"><v-card-text><p>{{allActual?'将转换所有配置媒体库中的实际视频，不受搜索或分页限制；复用30分钟内同范围的完整扫描，无有效扫描时先重扫。需要纳入刚新增文件时，请先重新扫描。':`将转换所选 ${actualSelection.length} 个实际视频。`}}直接创建长期分享，验证身份和分享播放后删除网盘源视频，保留分享、STRM和原播放地址。转换不创建临时目录或转存副本；后续播放时才按需转存。</p><p class="mt-3">分享可能因平台策略或源删除而失效。删源后分享验证失败会停止本批后续虚拟化，请核实分享；未知写结果不会自动重试。</p><v-checkbox v-model="purgeSource" :disabled="purgeEnabled" label="同时永久删除本次源文件对应的回收站条目（不可恢复）" color="error" hide-details class="mt-4"/><p class="p115-note mt-2">只清理本次删除且身份核实一致的条目，不清空回收站，也不补删历史文件。{{purgeEnabled?'全局永久删除设置已开启，本次同步执行。':'本次选择不改变自动清理的全局设置。'}}</p><v-alert v-if="(purgeSource || purgeEnabled) && !recycleKeyConfigured" type="warning" variant="tonal" class="mt-3">请先在存储设置填写六位回收站安全密钥并保存。<v-btn variant="text" @click="virtualDialog=false;emit('configure')">打开存储设置</v-btn></v-alert><v-checkbox v-model="consent" label="我授权删除这些资源的网盘源视频，并保留分享及STRM" color="primary" hide-details class="mt-4"/></v-card-text><v-card-actions><v-btn @click="virtualDialog=false;consent=false">取消</v-btn><v-spacer/><v-btn color="primary" :disabled="!consent || blocked || ((purgeSource || purgeEnabled) && !recycleKeyConfigured) || (!allActual && !actualSelection.length)" @click="virtualize">创建分享并虚拟化</v-btn></v-card-actions></v-card></v-dialog>
  <v-dialog v-model="shareDialog" max-width="600" @update:model-value="value=>{if(!value)share=null}"><v-card title="分享信息"><v-card-text><v-text-field :model-value="share?.url" label="分享链接" readonly variant="outlined"/><v-text-field :model-value="share?.password" label="提取码" readonly variant="outlined"/></v-card-text><v-card-actions><v-spacer/><v-btn @click="shareDialog=false;share=null">关闭</v-btn></v-card-actions></v-card></v-dialog>
</section>
</template>
