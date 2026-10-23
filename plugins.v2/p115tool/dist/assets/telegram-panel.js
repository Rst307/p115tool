import {importShared} from './__federation_fn_import-E6wRZccp.js';
import {p as request, r as requestError} from './ui-8x_RroF5.js';
const {h, ref, watch, nextTick, onMounted, onUnmounted} = await importShared('vue');
let nextId=0;

const labels = {
  DISABLED:'订阅已关闭', CONNECTING:'正在连接', LOGIN_REQUIRED:'请登录 Telegram 账号',
  CODE_SENT:'验证码已发送，请在 Telegram 或短信中查看', CODE_COOLDOWN:'请等待60秒后再发送验证码',
  PASSWORD_REQUIRED:'此账号开启了二步验证，请填写密码后登录', AUTHORIZED:'登录成功',
  ALREADY_AUTHORIZED:'账号会话已登录，无需重新发送验证码',
  READY:'账号已就绪', WATCHING:'正在订阅', CHANNEL_ATTENTION:'部分频道需要检查',
  CHANNEL_NOT_JOINED:'未找到已加入的频道，请检查账号和频道地址', CHANNEL_UNAVAILABLE:'频道读取失败',
  CODE_INVALID:'验证码不正确', CODE_EXPIRED:'验证码已过期，请重新获取', PASSWORD_INVALID:'二步验证密码不正确',
  PHONE_INVALID:'手机号码无效', API_INVALID:'API ID 或 API Hash 无效', CONFIG_INVALID:'订阅配置无效，请重新保存',
  USER_ACCOUNT_REQUIRED:'需要普通 Telegram 账号', DEPENDENCY_MISSING:'缺少依赖，请重新安装插件并重启 MoviePilot',
  RATE_LIMITED:'Telegram 要求限流等待，插件稍后再读取，请勿连续尝试登录',
  TELEGRAM_UNAVAILABLE:'连接失败，请检查网络、代理、API凭证和账号会话',
  WEB_CHANNEL_UNAVAILABLE:'公开网页不可用，请检查频道名；私有或受限制频道无法用网页模式读取',
  WEB_REQUEST_FAILED:'公开网页连接失败，请检查MoviePilot网络或Telegram代理',
  WEB_PAGE_INVALID:'公开网页内容无法识别，频道进度已保留，请稍后重试',
  WEB_PAGE_TOO_LARGE:'公开网页超过读取大小上限，频道进度已保留',
  WEB_HISTORY_LIMIT:'首次回看需要读取过多网页，请减少回看数量后再保存',
  PENDING:'待导入', QUEUED:'已排队', RUNNING:'导入中', DONE:'已完成', PARTIAL:'部分失败', FAILED:'失败',
  INTERRUPTED:'已中断', RESTARTED:'重启后需手动重试', TASK_UNAVAILABLE:'任务结果不可用，需手动核实后重试',
  EXTRACTION_AMBIGUOUS:'消息包含多个提取码，请手动复制链接和提取码到存储页导入',
  VIRTUAL_ORGANIZE_FAILED:'识别、分享验证或STRM输出失败，可核实后重试', STORAGE_FAILED:'分享读取或导入失败',
  SHARE_VIDEO_LIMIT:'分享超过1万个视频，请拆分导入', START_FAILED:'导入任务启动失败',
  SAVED:'订阅设置已保存', POLL_SCHEDULED:'已安排检查新消息', RETRY_QUEUED:'已安排重新导入',
  RETRY_UNAVAILABLE:'此记录当前无法重试，请刷新后核实',
  RETRY_QUEUE_FULL:'待处理队列已满，请等待任务完成后再重试',
  NETWORK_CHECK_SCHEDULED:'已安排梯子检测，请稍候查看检测结果',
  NETWORK_CHECK_COOLDOWN:'请等待30秒后再检测',
  NETWORK_TIMEOUT:'连接超时，请检查梯子和 MoviePilot 网络',
  NETWORK_FAILED:'连接失败，请检查代理地址、认证和 MoviePilot 网络',
  API_REQUIRED:'请先保存 API ID 和 API Hash，再检测账号连接',
};
const label = value => labels[value] || '请检查订阅或导入状态';
const networkLabels={UNKNOWN:'尚未检测',CHECKING:'正在检测…',AVAILABLE:'可用',UNAVAILABLE:'不可用',
  SETTINGS_REQUIRED:'需要配置',RATE_LIMITED:'限流等待，暂时无法判断'};
const historyStates=['ALL','PENDING','QUEUED','RUNNING','DONE','PARTIAL','FAILED','INTERRUPTED'];
const dateLabel=value=>value?new Date(value*1000).toLocaleString():'未记录';

export default {
  name:'TelegramSubscriptions', props:{api:Object,active:{type:Boolean,default:true}},
  setup(props) {
    const loading=ref(false), busy=ref(false), loaded=ref(false), section=ref('connection');
    const sections=[['connection','连接设置'],['subscription','频道订阅'],['imports','导入记录']];
    const id=`p115-telegram-${nextId++}`;
    const status=ref(null), error=ref(''), notice=ref('');
    const form=ref({enabled:false,mode:'web',api_id:'',api_hash:'',channels:'',interval:60,history_limit:0,proxy:'',clear_proxy:false,auto_upgrade:true});
    const phone=ref(''), code=ref(''), password=ref('');
    const historySearch=ref(''), historyState=ref('ALL');
    let historyPage=1, appliedSearch='', appliedState='ALL';
    let timer, disposed=false;
    async function refresh(initial=false) {
      if (loading.value || disposed) return;
      loading.value=true;
      try {
        const result=await request(props.api,'data',{kind:'telegram',page:historyPage,state:appliedState,search:appliedSearch});
        if(disposed) return;
        status.value=result; historyPage=result.page||1; error.value='';
        if(initial || !loaded.value) {
          const settings=result.settings;
          form.value={...form.value,...settings,api_hash:'',proxy:'',channels:settings.channels.join('\n'),clear_proxy:false};
          loaded.value=true;
        }
      } catch(e) {if(!disposed) error.value=requestError(e,'订阅状态读取失败，请先启用115工具箱并重新打开此面板。');}
      finally {loading.value=false;}
    }
    async function action(action, payload={}) {
      if(busy.value) return;
      busy.value=true; notice.value=''; error.value='';
      try {
        const result=await request(props.api,'action',{action,...payload});
        notice.value=label(result.error || result.state);
        if(result.state==='RETRY_BATCH_QUEUED') {
          notice.value=`已安排重试 ${result.queued} 项${result.remaining?`，另有 ${result.remaining} 项待队列空闲后再次重试`:''}${result.subscription_enabled?'':'；订阅已关闭，启用后处理'}`;
        }
        if(result.state==='RETRY_UNAVAILABLE' && action==='telegram_retry_all') notice.value='没有可重试的失败导入';
        if(result.state==='AUTHORIZED') {phone.value='';code.value='';password.value='';}
        if(result.state==='SAVED') {form.value.api_hash='';form.value.proxy='';form.value.clear_proxy=false;}
        await refresh();
      } catch(e) {error.value=requestError(e,'操作未完成，请检查输入和插件状态。');}
      finally {busy.value=false;}
    }
    function save() {
      const f=form.value;
      action('telegram_settings',{settings:{enabled:f.enabled,mode:f.mode,api_id:f.api_id || 0,api_hash:f.api_hash.trim(),
        channels:f.channels.split(/[\n,，]/).map(s=>s.trim()).filter(Boolean),interval:f.interval,
        history_limit:f.history_limit,proxy:f.proxy.trim(),clear_proxy:f.clear_proxy,auto_upgrade:f.auto_upgrade}});
    }
    function filterHistory(page=1) {
      if(loading.value) return;
      historyPage=page;appliedSearch=historySearch.value.trim();appliedState=historyState.value;
      refresh();
    }
    function historyNames(item) {
      const names=item.media_names||[];
      return h('td',{class:'p115-tg-names'},[
        h('strong',item.title||'未记录频道标题'),
        names.length?h('details',[h('summary',`识别名称 / 季集（${names.length}${item.total>names.length?'项摘要':'项'}）`),
          h('ul',names.map(name=>h('li',name))),item.total>names.length?h('small',`分享共 ${item.total} 个视频，最多展示10项名称`):null]):h('small','暂无识别名称'),
        h('small',`${item.channel} / 消息 ${item.message_id}`),
      ]);
    }
    function button(text, click, disabled=false) {
      return h('button',{type:'button',class:'p115-tg-button',disabled:busy.value||disabled,onClick:click},text);
    }
    function input(title, key, options={}) {
      const {hint,...attrs}=options;
      return h('label',{class:'p115-tg-field'},[
        h('span',title),h('input',{...attrs,'aria-label':title,value:form.value[key],disabled:busy.value,
          onInput:e=>form.value[key]=e.target.value}),hint?h('small',hint):null]);
    }
    function selectSection(event,index) {
      let target;
      if(event.key==='ArrowRight') target=(index+1)%sections.length;
      if(event.key==='ArrowLeft') target=(index+sections.length-1)%sections.length;
      if(event.key==='Home') target=0;
      if(event.key==='End') target=sections.length-1;
      if(target===undefined) return;
      event.preventDefault();section.value=sections[target][0];
      const tabs=event.currentTarget.parentElement;
      nextTick(()=>tabs.querySelectorAll('[role="tab"]')[target]?.focus());
    }
    function networkStatus() {
      const network=status.value.network || {state:'UNKNOWN'};
      return h('div',{class:['p115-tg-network',`p115-tg-network-${network.state.toLowerCase()}`]},[
        h('p',{role:'status','aria-live':'polite'},[
          h('strong',`梯子状态：${networkLabels[network.state] || '尚未检测'}`),
          h('small',`${network.via_proxy?'已保存代理':'服务器网络（未配置代理）'} · ${network.mode==='account'?'Telegram API':'Telegram 网页'}`),
          network.checked_at?h('small',`检测时间：${new Date(network.checked_at*1000).toLocaleString()} · 耗时 ${network.latency_ms} ms`):null,
          network.error?h('small',label(network.error)):null,
        ]),
        button(network.state==='CHECKING'?'检测中…':'检测梯子',()=>action('telegram_check_network'),network.state==='CHECKING'),
        h('small','检测 MoviePilot 服务器到 Telegram 的连通性，使用已保存的订阅方式和代理；修改后请先保存。未配置代理时可检测系统 VPN／直连。启用订阅后每5分钟复检；可用不代表账号已登录或所有频道均可读取。'),
      ]);
    }
    watch(()=>props.active,active=>{if(active) refresh();});
    onMounted(()=>{if(props.active) refresh();timer=setInterval(()=>{if(props.active) refresh();},5000);});
    onUnmounted(()=>{disposed=true;clearInterval(timer);});
    return ()=>h('section',{class:'p115-ui p115-tg'},[
        h('nav',{class:'p115-tg-tabs',role:'tablist','aria-label':'Telegram 板块'},sections.map(([key,title],index)=>
          h('button',{type:'button',role:'tab',id:`${id}-tab-${key}`,'aria-controls':`${id}-panel-${key}`,
            'aria-selected':section.value===key,tabindex:section.value===key?0:-1,
            onClick:()=>section.value=key,onKeydown:event=>selectSection(event,index)},title))),
        error.value?h('p',{role:'alert',class:'p115-tg-error'},error.value):null,
        notice.value?h('p',{role:'status',class:'p115-tg-notice'},notice.value):null,
        status.value?h('div',{class:'p115-tg-status'},[
          h('p',{'aria-live':'polite'},`${label(status.value.state)} · 待处理 ${status.value.pending} 项${status.value.last_poll?' · 最近检查 '+new Date(status.value.last_poll*1000).toLocaleString():''}`),
          button('刷新状态',()=>refresh(),loading.value),
        ]):null,
        loading.value&&!loaded.value?h('p','正在读取设置…'):null,
        loaded.value?h('div',{role:'tabpanel',id:`${id}-panel-${section.value}`,'aria-labelledby':`${id}-tab-${section.value}`},[
          section.value==='connection'?h('div',[
          h('h3','连接设置'),
          h('label',{class:'p115-tg-field'},[h('span','订阅方式'),h('select',{'aria-label':'订阅方式',value:form.value.mode,disabled:busy.value,
            onChange:event=>{form.value.mode=event.target.value;phone.value='';code.value='';password.value='';}},[
              h('option',{value:'web'},'公开网页（免 API、免登录）'),
              h('option',{value:'account'},'Telegram 账号（需要 API）'),
            ])]),
          h('p',form.value.mode==='web'?'直接读取公开频道网页，不需要申请 API 或登录 Telegram。保存连接设置后，到“频道订阅”添加公开频道并启用。':'先保存 API 凭证，再登录用于读取频道的普通 Telegram 账号。'),
          form.value.mode==='account'?h('div',{class:'p115-tg-grid'},[
            input('Telegram API ID','api_id',{inputmode:'numeric',hint:'在 my.telegram.org 的 API development tools 获取。'}),
            input('Telegram API Hash','api_hash',{type:'password',autocomplete:'new-password',placeholder:status.value.settings.api_hash_set?'已保存；留空保留':'32位 API Hash'}),
          ]):null,
          input('Telegram 代理（可选）','proxy',{type:'password',autocomplete:'new-password',placeholder:status.value.settings.proxy_set?'已保存；留空保留':'socks5://host:1080 或 http://host:8080',hint:'填写 MoviePilot 容器能访问的地址，支持用户名与密码；代理只用于 Telegram。'}),
          h('label',{class:'p115-tg-check'},[h('input',{type:'checkbox',checked:form.value.clear_proxy,onChange:e=>form.value.clear_proxy=e.target.checked}),'清除已保存的代理']),
          h('div',{class:'p115-tg-actions'},[button('保存连接设置',save)]),
          networkStatus(),
          form.value.mode==='account'?h('fieldset',{class:'p115-tg-login'},[
            h('legend',status.value.authorized?'Telegram 账号已登录':'登录 Telegram 账号'),
            h('label',{class:'p115-tg-field'},[h('span','手机号（含国家区号）'),h('input',{type:'tel',value:phone.value,autocomplete:'off',placeholder:'+8613800000000',onInput:e=>phone.value=e.target.value})]),
            button('发送验证码',()=>action('telegram_send_code',{phone:phone.value.trim()}),!phone.value),
            h('div',{class:'p115-tg-grid'},[
              h('label',{class:'p115-tg-field'},[h('span','验证码'),h('input',{inputmode:'numeric',value:code.value,autocomplete:'one-time-code',onInput:e=>code.value=e.target.value})]),
              h('label',{class:'p115-tg-field'},[h('span','二步验证密码（提示需要时填写）'),h('input',{type:'password',value:password.value,autocomplete:'new-password',onInput:e=>password.value=e.target.value})]),
            ]),
            button('登录',()=>action('telegram_sign_in',{code:code.value.trim(),password:password.value}),!code.value&&!password.value),
          ]):null,
          ]):null,
          section.value==='subscription'?h('div',[
          h('h3','频道订阅'),
          h('p','自动提取115分享、虚拟整理并生成STRM。导入时不转存视频，播放时才临时转存。'),
          h('label',{class:'p115-tg-check'},[h('input',{type:'checkbox',checked:form.value.enabled,onChange:e=>form.value.enabled=e.target.checked}),'启用频道订阅']),
          h('label',{class:'p115-tg-check'},[h('input',{type:'checkbox',checked:form.value.auto_upgrade,onChange:e=>form.value.auto_upgrade=e.target.checked}),'自动替换更高画质资源']),
          h('p','同一TMDB电影或同季同集先比较清晰度，同清晰度按 REMUX > Blu-ray > WEB-DL > WEBRip > HDTV。未知画质、不同剪辑／HDR／配音版本不互换，不按大小或编码猜测。新分享只读验证通过后切换来源，保留原STRM路径和播放地址；旧来源留作备用，导入不转存、不删除视频。'),
          h('label',{class:'p115-tg-field'},[h('span','频道（每行一个，最多30个）'),h('textarea',{'aria-label':'频道（每行一个，最多30个）',rows:4,value:form.value.channels,placeholder:form.value.mode==='web'?'@channel_name\nhttps://t.me/channel_name':'@channel_name\nhttps://t.me/channel_name\n-1001234567890',onInput:e=>form.value.channels=e.target.value}),h('small',form.value.mode==='web'?'只需填写公开频道名或频道网址，无需加入频道。支持 t.me/s/频道名；私有频道、邀请链接及受限网页不可读取。':'请先在此账号中加入频道。私有频道填写 -100 开头的频道ID；不接受邀请链接，不会自动加入。')]),
          h('div',{class:'p115-tg-grid'},[
            input('检查间隔（秒）','interval',{type:'number',min:30,max:3600}),
            input('首次回看消息数','history_limit',{type:'number',min:0,max:500,hint:'默认0：从首次成功检查时的最新消息开始；1–500：首次导入最近消息。已有进度的频道继续读取新消息。'}),
          ]),
          form.value.mode==='web'?h('p','网页模式按页续读新消息；频道不存在、网页受限或解析失败时保留进度并提示。消息编辑及网页未公开的内容不自动获取。'):null,
          h('div',{class:'p115-tg-actions'},[button('保存订阅设置',save),button('立即检查',()=>action('telegram_poll'),!status.value.settings.enabled)]),
          status.value.channels.length?h('ul',status.value.channels.map(c=>h('li',{key:c.channel},`${c.channel}：${label(c.state)}`))):null,
          ]):null,
          section.value==='imports'?h('div',[
          h('h3','导入记录'),
          h('p','频道标题供参考，展开可查看实际识别的剧名、季集和文件名称。相同分享与提取码只登记一次；一键重试覆盖全部历史可重试项，不受当前筛选限制。'),
          h('div',{class:'p115-tg-counts','aria-label':'全部历史状态统计'},historyStates.slice(1).map(key=>h('span',`${label(key)} ${status.value.counts?.[key]||0}`))),
          h('form',{class:'p115-tg-filters',onSubmit:e=>{e.preventDefault();filterHistory();}},[
            h('label',{class:'p115-tg-field'},[h('span','搜索导入记录'),h('input',{'aria-label':'搜索导入记录',placeholder:'剧名、季集、频道或消息编号',maxlength:100,value:historySearch.value,onInput:e=>historySearch.value=e.target.value})]),
            h('label',{class:'p115-tg-field'},[h('span','记录状态'),h('select',{'aria-label':'记录状态',value:historyState.value,onChange:e=>historyState.value=e.target.value},historyStates.map(key=>h('option',{value:key},key==='ALL'?'全部状态':label(key))))]),
            button('筛选记录',()=>filterHistory(),loading.value),
          ]),
          h('div',{class:'p115-tg-actions'},[button(`一键重试失败项（${status.value.retryable||0}）`,()=>action('telegram_retry_all'),!status.value.retryable)]),
          !status.value.settings.enabled?h('p','订阅已关闭，重试项入队后需启用频道订阅才能处理。'):null,
          status.value.items.length?h('div',{class:'p115-tg-table-scroll'},[h('table',{class:'p115-tg-table'},[
            h('thead',[h('tr',['剧集 / 来源','状态','成功 / 失败','时间 / 尝试','操作'].map(text=>h('th',text)))]),
            h('tbody',status.value.items.map(item=>h('tr',{key:item.id},[
              historyNames(item),
              h('td',[h('span',{class:['p115-tg-badge',`p115-tg-state-${item.state.toLowerCase()}`]},label(item.state)),
                item.state==='RUNNING'?h('small',item.phase==='SCANNING'?`扫描中 · 已发现 ${item.found||0} 个视频`:`已处理 ${item.done+item.failed}${item.total!=null?' / '+item.total:''}`):null,
                item.error?h('small',label(item.error)):null]),
              h('td',[`${item.done} / ${item.failed}`,item.total!=null?h('small',`视频总数 ${item.total}`):null,h('small',`升级 ${item.upgraded||0} · 保留 ${item.skipped||0}`)]),
              h('td',[h('small',`收录 ${dateLabel(item.created_at)}`),h('small',`开始 ${dateLabel(item.started_at)}`),h('small',`结束 ${dateLabel(item.finished_at)}`),h('small',`尝试 ${item.attempts||0} 次`)]),
              h('td',[item.message_url?h('a',{href:item.message_url,target:'_blank',rel:'noopener noreferrer'},'原消息'):null,
                item.retryable?button('重试',()=>action('telegram_retry',{id:item.id})):null]),
            ]))),
          ])]):h('p',appliedSearch||appliedState!=='ALL'?'没有符合条件的导入记录':'暂无导入记录'),
          h('div',{class:'p115-tg-actions'},[
            button('上一页',()=>filterHistory(historyPage-1),loading.value||historyPage<=1),
            h('span',`第 ${status.value.page||1} / ${Math.max(1,Math.ceil((status.value.total||0)/50))} 页 · 共 ${status.value.total||0} 项`),
            button('下一页',()=>filterHistory(historyPage+1),loading.value||historyPage*50>=(status.value.total||0)),
          ]),
          ]):null,
        ]):null,
    ]);
  },
};
