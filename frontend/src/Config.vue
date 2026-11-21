<script setup>
import { ref, computed, watch, onBeforeUnmount } from 'vue'
import { post, saveConfiguration, requestErrorMessage } from './host'
import { configurationSection } from './navigation'
import './ui.css'
const props=defineProps({initialConfig:{type:Object,default:()=>({})},api:Object})
const emit=defineEmits(['save','close','switch'])
const tab=configurationSection
const initial=props.initialConfig
let sources=initial.source_cids || []
try { if(initial.source_cids_json) sources=JSON.parse(initial.source_cids_json) } catch {}
const cloneSources=items=>Array.isArray(items)?items.map(item=>typeof item==='object'?{...item}:item):[]
const config=ref({enabled:false,use_moviepilot_auth:false,strm_dir:'/opt/emby/strm',public_url:'http://localhost:3000',scheduled:false,organize_scheduled:false,organize_time:'02:00',organize_target_cid:'',auto_after_transfer:false,scan_time:'03:00',request_timeout:30,temp_cid:'',temp_days:7,temp_cleanup:true,recycle_purge:false,recycle_password:'',recycle_empty_scheduled:false,recycle_empty_time:'04:00',...Object.fromEntries(['enabled','use_moviepilot_auth','strm_dir','public_url','scheduled','organize_scheduled','organize_time','organize_target_cid','auto_after_transfer','scan_time','request_timeout','temp_cid','temp_days','temp_cleanup','recycle_purge','recycle_password','recycle_empty_scheduled','recycle_empty_time'].filter(k=>initial[k]!==undefined).map(k=>[k,initial[k]])),source_cids:cloneSources(sources),organize_cids:cloneSources(initial.organize_cids)})
config.value.cookie_auto_refresh = initial.cookie_auto_refresh === true
const cookieDraft=ref(''), cookieChanged=ref(false), cookieSaved=ref(Boolean(initial.cookie_set || initial.cookie))
const renewalSeed=ref(''), renewalSeedChanged=ref(false), seedSaved=ref(initial.cookie_renewal_seed_set===true)
const renewalStatus=ref(null)
const renewalLabels={UNCHECKED:'等待首次检查',VALID:'主 Cookie 有效',RENEWED:'已自动续登',
  STATUS_UNAVAILABLE:'无法核实登录状态',INVALID_PAIR:'检查两份 Cookie 是否属于同一账号、不同设备',
  BACKUP_EXPIRED:'备用 Cookie 已失效，请重新登录后更新',RENEWAL_FAILED:'续登失败，请检查备用 Cookie',
  COOLDOWN:'续登限频中，稍后自动检查',PERSISTENCE_FAILED:'无法保存续登记录，请检查插件数据目录权限'}
watch(tab,async next=>{
  if(next!=='connection' || initial.enabled!==true || initial.use_moviepilot_auth===true || initial.cookie_auto_refresh!==true)return
  try { renewalStatus.value=await post(props.api,'data',{kind:'cookie_status'}) } catch { renewalStatus.value=null }
},{immediate:true})
const error=ref(''), busy=ref(false), picker=ref(false), folders=ref([]), trail=ref([{cid:'0',name:'115网盘'}]), folderBusy=ref(false)
const loopbackAddress=computed(()=>{try{return ['localhost','127.0.0.1','[::1]'].includes(new URL(config.value.public_url).hostname)}catch{return false}})
const groups=[{field:'source_cids',title:'STRM生成源文件夹',hint:'选择整理后的媒体库，如 /emby，用于扫描和生成STRM。'},{field:'organize_cids',title:'MoviePilot待整理文件夹',hint:'选择未整理资源，如 /下载/待整理，用于手动及定时整理。整理后移动到下方指定的STRM媒体库。'}]
const visibleGroups=computed(()=>groups.filter(group=>['organize','strm'].includes(tab.value) && group.field===(tab.value==='organize'?'organize_cids':'source_cids')))
const folderField=ref('source_cids')
const cid=ref({source_cids:'',organize_cids:''}), prefix=ref({source_cids:'/',organize_cids:'/'})
const manualOpen=ref({source_cids:false,organize_cids:false}), folderError=ref(''), saved=ref(false)
watch(config,()=>{saved.value=false},{deep:true,flush:'sync'})
const scanOpen=ref(false), scanState=ref(''), scanImage=ref(''), scanMessage=ref('')
const scanFinished=computed(()=>['SAVED','PRIMARY_ONLY'].includes(scanState.value))
const scanPending=['STARTING','WAITING','SCANNED','POLL_UNAVAILABLE']
const scanLabels={
  STARTING:'正在获取二维码…', WAITING:'使用手机上的 115 App 扫码。', SCANNED:'已扫码，请在手机上确认登录。',
  POLL_UNAVAILABLE:'暂时无法读取扫码状态，正在重试…', BUSY:'已有扫码正在等待或处理，请稍后重试。',
  EXPIRED:'二维码已过期，请重新获取。', CANCELED:'本次扫码已取消。',
  CONFIG_CHANGED:'插件设置已变更，请重新打开设置后扫码。',
  ACCOUNT_MISMATCH:'扫码账号与插件当前已配置的 115 账号不同。',
  QR_UNAVAILABLE:'二维码获取失败，请检查 MoviePilot 与 115 的网络连接后重试。',
  LOGIN_FAILED:'115 登录结果未通过验证，请重新扫码。', SAVE_FAILED:'Cookie 保存失败，请检查插件配置和数据目录。',
  SAVED:'主 Cookie 和备用 Cookie 已保存，自动更新已开启。',
  PRIMARY_ONLY:'主 Cookie 已保存。115 未发放可验证的备用会话，自动更新暂未开启。'
}
let scanGeneration=0, scanId='', scanTimer, scanDeadline=0
function cancelScan(id) {
  if(id)post(props.api,'cookie_qr',{action:'cancel',scan_id:id}).catch(()=>{})
}
function closeScan() {
  scanGeneration+=1
  clearTimeout(scanTimer)
  if(!scanFinished.value && scanPending.includes(scanState.value))cancelScan(scanId)
  scanId='';scanImage.value='';scanOpen.value=false
}
onBeforeUnmount(closeScan)
async function startScan() {
  closeScan()
  const generation=scanGeneration
  scanOpen.value=true;scanState.value='STARTING';scanMessage.value=''
  try {
    const result=await post(props.api,'cookie_qr',{action:'start'})
    if(generation!==scanGeneration) { cancelScan(result.scan_id);return }
    scanState.value=result.state;scanImage.value=result.image || '';scanId=result.scan_id || ''
    if(scanId) {
      scanDeadline=Date.now()+180000
      scanTimer=setTimeout(()=>pollScan(generation),800)
    }
  } catch(e) {
    if(generation!==scanGeneration)return
    scanState.value='QR_UNAVAILABLE';scanMessage.value=requestErrorMessage(e,scanLabels.QR_UNAVAILABLE)
  }
}
async function pollScan(generation) {
  let result
  try { result=await post(props.api,'cookie_qr',{action:'poll',scan_id:scanId}) }
  catch(e) {
    if(e.status===401 || e.status===403 || e.status===404) {
      if(generation!==scanGeneration)return
      cancelScan(scanId)
      scanMessage.value=requestErrorMessage(e,scanLabels.QR_UNAVAILABLE)
      result={state:'QR_UNAVAILABLE'}
    } else {
      result={state:'POLL_UNAVAILABLE'}
    }
  }
  if(generation!==scanGeneration)return
  if(result.state!=='BUSY')scanState.value=result.state
  if(scanPending.includes(scanState.value)) {
    if(Date.now()<scanDeadline) {
      scanTimer=setTimeout(()=>pollScan(generation),1600)
      return
    }
    cancelScan(scanId);scanState.value='EXPIRED'
  }
  scanImage.value=''
  if(!scanFinished.value)return
  cookieSaved.value=true;cookieDraft.value='';cookieChanged.value=false
  seedSaved.value=result.state==='SAVED';renewalSeed.value='';renewalSeedChanged.value=false
  config.value.cookie_auto_refresh=seedSaved.value;renewalStatus.value=null
  if(config.value.use_moviepilot_auth)scanMessage.value='要使用分享导入和新转存，请关闭 MoviePilot 原生授权开关并保存配置。'
  if(result.schedule_updated===false)scanMessage.value='定时任务刷新失败，请重新保存配置。'
}
function add(source,field) { if (!config.value[field].some(x=>String(x.cid ?? x)===source.cid)) config.value[field].push(source) }
function manual(field) {
  error.value='';saved.value=false
  const id=cid.value[field].trim(), path=prefix.value[field].trim()
  if (!/^\d{1,20}$/.test(id) || !path.startsWith('/')) { error.value='目录ID需为数字，完整网盘路径需以 / 开头。';return }
  if(config.value[field].some(x=>String(x.cid ?? x)===id)) {error.value='该目录已在列表中，无需重复添加。';return}
  add({cid:id,prefix:path},field);cid.value[field]=''
}
async function browse() {
  folderBusy.value=true;folderError.value='';folders.value=[]
  try { folders.value=(await post(props.api,'data',{kind:'folders',cid:trail.value.at(-1).cid})).items }
  catch(e) { folderError.value=requestErrorMessage(e,'目录读取失败。请检查115授权、保存连接设置并启用插件。') }
  finally { folderBusy.value=false }
}
function open(field) { folderField.value=field;picker.value=true;trail.value=[{cid:'0',name:'115网盘'}];browse() }
function enter(item) { trail.value.push(item);browse() }
function back() { if(trail.value.length>1) { trail.value.pop();browse() } }
function select() { if(folderField.value==='temp_cid')config.value.temp_cid=trail.value.at(-1).cid;else add({cid:trail.value.at(-1).cid,prefix:'/'+trail.value.slice(1).map(x=>x.name).join('/')},folderField.value);picker.value=false }
async function save() {
  if(busy.value)return
  busy.value=true;error.value='';saved.value=false
  try {
    const payload={...config.value}
    if(cookieChanged.value)payload.cookie=cookieDraft.value.trim()
    if(renewalSeedChanged.value)payload.cookie_renewal_seed=renewalSeed.value.trim()
    await saveConfiguration(props.api,payload,v=>emit('save',v))
    if(cookieChanged.value)cookieSaved.value=Boolean(payload.cookie)
    if(renewalSeedChanged.value)seedSaved.value=Boolean(payload.cookie_renewal_seed)
    cookieDraft.value='';cookieChanged.value=false;renewalSeed.value='';renewalSeedChanged.value=false;saved.value=true
  }
  catch(e) {
    error.value=requestErrorMessage(e,'配置无效，请检查目录ID、路径和播放地址。')
    if(config.value.cookie_auto_refresh && !config.value.use_moviepilot_auth) error.value+=' 自动更新需要同一 115 账号、不同设备的两份 Cookie。'
  }
  finally { busy.value=false }
}
</script>
<template>
<section class="p115-ui p115-config">
  <header class="p115-header">
    <div><div class="p115-eyebrow">115 工具箱 · 设置</div><h2>按功能配置你的工具箱</h2><p>整理、STRM、存储和通用连接分别配置；切换板块会保留未保存的修改。</p></div>
    <v-btn variant="text" @click="emit('close')">关闭</v-btn>
  </header>
  <v-alert v-if="error" type="error" variant="tonal" class="mb-4" role="alert">{{error}}</v-alert>
  <v-tabs v-model="tab" color="primary" class="mb-4" aria-label="配置板块"><v-tab value="organize">整理</v-tab><v-tab value="strm">STRM</v-tab><v-tab value="storage">存储</v-tab><v-tab value="connection">通用连接</v-tab></v-tabs>
  <section v-if="tab==='storage'" class="p115-section">
    <h3>实际存储与虚拟存储</h3><p class="p115-note mt-2 mb-4">实际视频长期保留。虚拟资源保留分享和STRM，使用临时转存副本播放，副本到期清理后按需重新转存。</p>
    <v-alert v-if="config.use_moviepilot_auth" type="info" variant="tonal">原生授权可以播放已核实的临时副本。分享导入、转存、清理和回收站设置在 Cookie 模式下生效。</v-alert>
    <template v-else>
    <v-text-field v-model="config.temp_cid" label="115临时副本目录ID" inputmode="numeric" variant="outlined" hint="请选择专用非根目录，插件会为每个资源创建独立子目录。" persistent-hint/>
    <v-btn variant="tonal" class="mt-3 mb-5" prepend-icon="mdi-folder-search-outline" @click="open('temp_cid')">选择临时副本目录</v-btn>
    <v-text-field v-model="config.temp_days" type="number" min="1" max="365" label="临时副本保留天数" variant="outlined" hint="1–365天，默认7天。从转存完成时起算；修改后也适用于已有副本。" persistent-hint/>
    <v-switch v-model="config.temp_cleanup" label="自动清理到期临时副本" color="primary" hide-details class="mt-4"/>
    <p class="p115-note mt-3">每小时15分检查。每次播放请求延后至少24小时清理；分享无法验证或副本身份变化时保留文件。只删除已记录的临时视频，保留空子目录。</p>
    <v-switch v-model="config.recycle_purge" label="删除后自动从回收站永久删除" color="primary" hide-details class="mt-4"/>
    <p class="p115-note mt-3">默认关闭。开启后仅处理插件本次实际转虚拟和临时副本清理删除的视频，永久删除后无法从回收站恢复。不会清空回收站或补删历史文件。无法核实身份、接口失败或结果未知时停止，并保留检查点，不自动重试。</p>
    <v-text-field v-model="config.recycle_password" type="password" maxlength="6" autocomplete="off" label="115回收站安全密钥" variant="outlined" hint="本次实际转虚拟选择永久删除时也需填写六位数字并保存，无需开启全局自动永久删除。" persistent-hint class="mt-4"/>
    <h3 class="mt-5">定时清空115回收站</h3>
    <v-alert type="warning" variant="tonal" class="mt-3">开启后每天永久删除执行时回收站内的全部条目，包括历史文件、手动删除及其他应用删除的文件，无法恢复。请确认回收站中没有需要保留的文件。</v-alert>
    <v-switch v-model="config.recycle_empty_scheduled" label="每天清空整个115回收站（不可恢复）" color="error" hide-details class="mt-4"/>
    <v-text-field v-model="config.recycle_empty_time" :disabled="!config.recycle_empty_scheduled" type="time" label="每日清空时间" hint="北京时间（Asia/Shanghai），默认04:00。需填写上方六位安全密钥。" persistent-hint variant="outlined" class="mt-4"/>
    <p class="p115-note mt-3">默认关闭，保存后生效。先记录条目快照，再分批永久删除并核实；执行过程中新增的条目留到下次。其他插件存储或STRM任务运行时跳过本次。结果未知时暂停后续清空，仅只读核实，不自动重试删除；可在存储页查看状态。</p>
    </template>
  </section>
  <div v-if="tab==='connection'" class="p115-section p115-enable">
    <div><h3>启用115工具箱</h3><p>开启后可生成STRM，并提供302直链播放。</p></div>
    <v-switch v-model="config.enabled" aria-label="启用115工具箱" color="primary" hide-details inset/>
  </div>
  <section v-if="tab==='connection'" class="p115-section">
    <div class="p115-section-heading"><div><h3>115连接</h3><p>保存连接方式后即可浏览网盘目录。</p></div></div>
    <v-switch v-model="config.use_moviepilot_auth" label="使用 MoviePilot 原生 115 授权" color="primary" hide-details class="mb-3"/>
    <v-alert v-if="config.use_moviepilot_auth" type="info" variant="tonal" class="mb-4">请先在 MoviePilot 的存储设置登录 115。原生授权支持目录扫描、生成 STRM，以及实际文件和已核实临时副本的播放。分享导入、新转存、Telegram 导入和回收站操作使用 Cookie 模式。</v-alert>
    <v-btn variant="tonal" color="primary" class="mb-4" prepend-icon="mdi-qrcode-scan" @click="startScan">扫码配置 Cookie</v-btn>
    <template v-if="!config.use_moviepilot_auth">
      <v-text-field v-model="cookieDraft" type="password" label="115 Cookie" autocomplete="off" variant="outlined" density="comfortable"
        :hint="cookieChanged && !cookieDraft ? '保存后清除已保存的 Cookie。' : cookieSaved ? '已保存主 Cookie，留空保留。' : '也可以手动填写主 Cookie。'" persistent-hint class="mb-3"
        @update:model-value="cookieChanged=true;saved=false"/>
      <v-btn v-if="cookieSaved && !cookieChanged" variant="text" class="mb-3" @click="cookieDraft='';cookieChanged=true;config.cookie_auto_refresh=false;saved=false">清除已保存的主 Cookie</v-btn>
      <v-switch v-model="config.cookie_auto_refresh" label="自动更新 115 Cookie" color="primary" hide-details class="mb-3"/>
      <template v-if="config.cookie_auto_refresh">
        <v-text-field v-model="renewalSeed" type="password" label="备用 115 Cookie" autocomplete="off" variant="outlined" density="comfortable"
          :hint="seedSaved ? '已保存备用 Cookie，留空保留。' : '填写同一账号在另一种设备登录的 Cookie。'" persistent-hint class="mb-3"
          @update:model-value="renewalSeedChanged=true;saved=false"/>
        <p class="p115-note mb-3">每半小时检查主 Cookie，确认失效后用备用会话续登。续登可能影响主 Cookie 对应设备的其他登录。</p>
        <p v-if="renewalLabels[renewalStatus?.state]" class="p115-note mb-3" aria-live="polite">{{renewalLabels[renewalStatus.state]}}<span v-if="renewalStatus.renewed_at"> · 上次续登 {{new Date(renewalStatus.renewed_at*1000).toLocaleString()}}</span></p>
      </template>
      <v-btn v-else-if="seedSaved && !renewalSeedChanged" variant="text" class="mb-3" @click="renewalSeed='';renewalSeedChanged=true;saved=false">清除已保存的备用 Cookie</v-btn>
    </template>
    <v-text-field v-model="config.request_timeout" type="number" min="5" max="120" label="115请求超时（秒）" hint="5–120秒，默认30秒" persistent-hint variant="outlined" density="comfortable"/>
  </section>
  <section v-if="tab==='strm'" class="p115-section">
    <h3 class="mb-4">输出与播放</h3>
    <div class="p115-grid">
      <v-text-field v-model="config.strm_dir" label="本地STRM输出目录" placeholder="/opt/emby/strm" variant="outlined" density="comfortable" hint="MoviePilot容器内的路径，需同时挂载给Emby。" persistent-hint/>
      <v-text-field v-model="config.public_url" label="MoviePilot播放服务地址" placeholder="http://moviepilot.example:3000" variant="outlined" density="comfortable" hint="填写Emby能访问的MoviePilot地址，不带 /#/plugins。" persistent-hint/>
    </div>
    <v-alert v-if="loopbackAddress" type="warning" variant="tonal" class="mt-5">当前是回环地址。MoviePilot与Emby在不同Docker容器时，请改为Emby能访问的MoviePilot局域网地址或域名。</v-alert>
    <div class="p115-note mt-5">修改播放地址后，保存配置并重新生成STRM。插件会更新已确认归属的旧播放地址，保留手工修改的文件。</div>
  </section>
  <section v-for="group in visibleGroups" v-show="tab!=='connection'" :key="group.field" class="p115-section">
    <div class="p115-section-heading"><div><h3>{{group.field === 'source_cids' ? 'STRM媒体库' : 'MoviePilot待整理目录'}} <span class="p115-count">{{config[group.field].length}} 个目录</span></h3><p>{{group.hint}}</p></div></div>
    <div v-if="!config[group.field].length" class="p115-empty"><v-icon size="28" color="primary">mdi-folder-outline</v-icon><div><strong>{{group.field === 'source_cids' ? '还没有选择媒体目录' : '暂未设置待整理目录'}}</strong><p>{{group.field === 'source_cids' ? '添加整理后的媒体库，生成时会递归扫描所有子目录。' : '需要整理时再添加，与STRM媒体库分别配置。'}}</p></div></div>
    <v-list v-else class="p115-folder-list" bg-color="transparent">
      <v-list-item v-for="(item,i) in config[group.field]" :key="String(item.cid ?? item)" prepend-icon="mdi-folder-outline" :title="item.prefix || '/'" :subtitle="`目录ID · ${item.cid ?? item}`">
        <template #append><v-btn variant="text" size="small" :aria-label="`移除目录 ${item.prefix || item.cid || item}`" @click="config[group.field].splice(i,1)">移除</v-btn></template>
      </v-list-item>
    </v-list>
    <div class="p115-actions mt-4"><v-btn variant="tonal" color="primary" prepend-icon="mdi-folder-search-outline" @click="open(group.field)">浏览115目录</v-btn><v-btn variant="text" :aria-expanded="manualOpen[group.field]" @click="manualOpen[group.field]=!manualOpen[group.field]">{{manualOpen[group.field] ? '收起手动添加' : '手动填写目录ID'}}</v-btn></div>
    <div v-if="manualOpen[group.field]" class="p115-manual mt-4"><div class="p115-grid"><v-text-field v-model="cid[group.field]" label="115目录ID" inputmode="numeric" variant="outlined" density="comfortable" hide-details/><v-text-field v-model="prefix[group.field]" label="完整网盘路径" placeholder="/emby/电视剧" variant="outlined" density="comfortable" hide-details/></div><v-btn variant="tonal" class="mt-3" @click="manual(group.field)">添加目录</v-btn></div>
    <div v-if="group.field==='source_cids'" class="p115-note mt-4">已有电影、电视剧及类别目录会沿用分类；其他文件交由MoviePilot识别。直接选中类别或影片目录，也会保留上层分类。</div>
  </section>
  <section v-if="tab==='strm'" class="p115-section">
    <div class="p115-section-heading"><div><h3>自动生成STRM</h3><p>手动生成随时可用，也可设置每天自动更新。</p></div></div>
    <v-switch v-model="config.auto_after_transfer" label="整理完成后自动生成STRM" color="primary" hide-details class="mb-2"/>
    <p class="p115-note mb-4">默认关闭。开启并保存后，检测MoviePilot成功整理到115，连续事件合并等待10秒，再扫描上方STRM媒体库。请确保生成源包含整理目标；扫描期间收到的新事件会补扫一次。</p>
    <v-switch v-model="config.scheduled" label="每天自动生成STRM" color="primary" hide-details class="mb-4"/>
    <div class="p115-grid"><v-text-field v-model="config.scan_time" :disabled="!config.scheduled" type="time" label="每日生成时间" hint="北京时间" persistent-hint variant="outlined" density="comfortable"/></div>
  </section>
  <section v-if="tab==='organize'" class="p115-section">
    <h3>整理目标媒体库</h3>
    <v-select v-model="config.organize_target_cid" :items="config.source_cids.map(item=>({cid:String(item.cid ?? item),title:item.prefix || '/'}))" item-title="title" item-value="cid" clearable @update:model-value="value=>config.organize_target_cid=value || ''" label="115 STRM媒体库目标" variant="outlined" class="mt-4" hint="从STRM板块配置的网盘媒体库中选择。仅一个媒体库时自动使用；多个时必须明确选择。" persistent-hint/>
    <p class="p115-note mt-3">目标是115网盘中保存视频的目录。本地STRM输出目录用于保存播放文件。MoviePilot继续负责识别、命名和分类，请在宿主配置相同的115媒体库。</p>
  </section>
  <section v-if="tab==='organize'" class="p115-section">
    <h3>定时整理</h3><p class="p115-note mt-2">每天把全部待整理目录交给MoviePilot，移动到指定的115 STRM媒体库，成功后原目录不再保留已移动文件。</p>
    <v-switch v-model="config.organize_scheduled" label="每天自动整理" color="primary" hide-details class="mb-4"/>
    <v-text-field v-model="config.organize_time" :disabled="!config.organize_scheduled" type="time" label="每日整理时间" hint="北京时间（Asia/Shanghai）" persistent-hint variant="outlined" density="comfortable"/>
    <v-alert v-if="config.organize_scheduled && !config.organize_cids.length" type="warning" variant="tonal" class="mt-4">请先添加待整理目录，空列表不会启动定时任务。</v-alert>
    <p class="p115-note mt-4">默认关闭，保存后生效。STRM生成中则跳过当次整理；提交结果未知会关闭定时整理，请核实宿主任务后再开启。整理进度在MoviePilot查看。</p>
  </section>
  <footer class="p115-savebar"><div aria-live="polite"><strong>{{saved ? '配置已交由MoviePilot保存' : '设置完成后，记得保存'}}</strong><p>{{saved ? '返回对应功能板块即可操作。' : '浏览目录需先保存连接设置并启用插件。'}}</p></div><v-btn color="primary" :loading="busy" :disabled="busy" prepend-icon="mdi-content-save-outline" @click="save">保存配置</v-btn></footer>
  <v-dialog :model-value="scanOpen" max-width="420" @update:model-value="value=>{if(!value)closeScan()}">
    <v-card class="p115-ui" title="115 扫码登录">
      <v-card-text>
        <p role="status" aria-live="polite">{{scanLabels[scanState]}}</p>
        <img v-if="scanImage" :src="scanImage" alt="115 登录二维码" width="208" height="208" class="d-block mx-auto my-4"/>
        <p v-if="scanImage" class="p115-note">确认后会保存主 Cookie 并尝试生成备用会话。同类型设备的其他登录可能受影响。</p>
        <p v-if="scanMessage" class="p115-note mt-3">{{scanMessage}}</p>
      </v-card-text>
      <v-card-actions>
        <v-spacer/>
        <v-btn v-if="!scanFinished && !scanPending.includes(scanState)" color="primary" @click="startScan">重新获取</v-btn>
        <v-btn @click="closeScan">{{scanPending.includes(scanState) ? '取消' : '关闭'}}</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog v-model="picker" max-width="640"><v-card class="p115-ui" :title="folderField === 'temp_cid' ? '选择临时副本目录' : folderField === 'organize_cids' ? '选择待整理目录' : '选择STRM媒体库'"><v-card-text>
    <div class="p115-note p115-path mb-3">{{trail.map(x=>x.name).join(' / ')}}</div>
    <div class="p115-actions mb-3"><v-btn :disabled="trail.length===1 || folderBusy" variant="text" prepend-icon="mdi-arrow-left" @click="back">上一级</v-btn><v-btn :disabled="folderBusy" variant="text" @click="browse">重新读取</v-btn></div>
    <v-progress-linear v-if="folderBusy" indeterminate color="primary" aria-label="正在读取目录"/>
    <v-alert v-if="folderError" type="error" variant="tonal" role="alert">{{folderError}}</v-alert>
    <div v-else-if="!folderBusy && !folders.length" class="p115-empty">当前目录没有子文件夹，可以选择当前目录。</div>
    <v-list class="p115-picker-list"><v-list-item v-for="item in folders" :key="item.cid" :title="item.name" prepend-icon="mdi-folder-outline" append-icon="mdi-chevron-right" :disabled="folderBusy" @click="enter(item)"/></v-list>
    <div v-if="trail.length===1" class="p115-note mt-3">{{folderField==='temp_cid' ? '临时副本不支持网盘根目录，请进入专用子目录。' : '当前位于网盘根目录；选择此处将递归处理整个网盘，建议进入具体媒体文件夹。'}}</div>
  </v-card-text><v-card-actions><v-btn @click="picker=false">取消</v-btn><v-spacer/><v-btn :disabled="folderBusy || !!folderError || (folderField==='temp_cid' && trail.length===1)" variant="tonal" color="primary" @click="select">选择当前目录</v-btn></v-card-actions></v-card></v-dialog>
</section>
</template>
