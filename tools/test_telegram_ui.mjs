// Usage: P115_UI_TEST_NODE_MODULES=/tmp/.../node_modules node tools/test_telegram_ui.mjs
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=fileURLToPath(new URL('../',import.meta.url));
const modules=process.env.P115_UI_TEST_NODE_MODULES;
if(!modules) throw new Error('Set P115_UI_TEST_NODE_MODULES to a temporary Vue + Playwright installation');
const require=createRequire(path.join(modules,'../package.json'));
const {chromium}=require('playwright');
const html=`<!DOCTYPE html><html lang="zh"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/vuetify.css"><link rel="stylesheet" href="/assets/ui-8ywdPfsL.css"><link rel="stylesheet" href="/assets/telegram.css"><script type="importmap">{"imports":{"vue":"/vue.js"}}</script><body><div id="app"></div><script type="module">
import * as Vue from '/vue.js';
import {createVuetify,components,directives} from '/vuetify.js';
import {init,get} from '/assets/remoteEntry.js';
init({vue:{'3.5.0':{get:()=>()=>Vue}}});
window.calls=[];
const data={settings:{enabled:false,mode:'web',api_id:0,api_hash_set:false,channels:[],interval:60,history_limit:0,proxy_set:false},state:'DISABLED',authorized:false,last_poll:null,channels:[],pending:1,items:[{id:1,channel:'-1001234567890',message_id:42,state:'FAILED',done:0,failed:1,error:'STORAGE_FAILED'}]};
const api={async post(url,payload){window.calls.push(payload);
 if(payload.kind==='bootstrap') return {enabled:true,error:null,ui_contract:3};
 if(payload.kind==='status') return {state:'IDLE',count:0};
 if(payload.kind==='organize_sources') return {items:[],scheduled:false,time:'02:00',target:null,paused:false};
 if(payload.kind==='sources') return {items:[]};
 if(payload.kind==='telegram') return structuredClone(data);
 if(payload.action==='telegram_settings'){data.settings={...payload.settings,api_hash_set:true,proxy_set:false};delete data.settings.api_hash;delete data.settings.proxy;return {state:'SAVED'};}
 if(payload.action==='telegram_send_code')return {state:'CODE_SENT'};
 if(payload.action==='telegram_sign_in'){if(!payload.password)return {state:'PASSWORD_REQUIRED'};data.authorized=true;return {state:'AUTHORIZED'};}
 if(payload.action==='telegram_retry')return {state:'RETRY_QUEUED'};
 return {state:'POLL_SCHEDULED'};
}};
const Panel=(await import('/assets/telegram-panel.js')).default;
window.wrappers={Config:(await get('./Config'))(),Page:(await get('./Page'))()};
window.events=[];
const view=new URL(location.href).searchParams.get('view');
const Target=view==='config'?window.wrappers.Config:view==='page'?window.wrappers.Page:Panel;
window.app=Vue.createApp({render:()=>Vue.h(components.VApp,[Vue.h(components.VMain,[Vue.h(Target,{api,initialConfig:{enabled:true,cookie:'initial-cookie'},onClose:()=>window.events.push('close'),onSwitch:()=>window.events.push('switch'),onSave:()=>window.events.push('save')})])])});
window.app.use(createVuetify({components,directives}));window.app.mount('#app');
</script></body></html>`;
const server=createServer(async(req,res)=>{
  try {
    let content,type;
    if(req.url.split('?')[0]==='/'){content=html;type='text/html';}
    else if(req.url==='/vue.js'){content=await readFile(path.join(modules,'vue/dist/vue.esm-browser.prod.js'));type='text/javascript';}
    else if(req.url==='/vuetify.js'){content=await readFile(path.join(modules,'vuetify/dist/vuetify.esm.js'));type='text/javascript';}
    else if(req.url==='/vuetify.css'){content=await readFile(path.join(modules,'vuetify/dist/vuetify.min.css'));type='text/css';}
    else if(/^\/assets\/[\w.-]+$/.test(req.url)){content=await readFile(path.join(root,'plugins.v2/p115tool/dist',req.url));type=req.url.endsWith('.css')?'text/css':'text/javascript';}
    else {res.writeHead(404);res.end();return;}
    res.writeHead(200,{'Content-Type':type});res.end(content);
  }catch{res.writeHead(500);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  browser=await chromium.launch({headless:true,executablePath:process.env.P115_UI_TEST_CHROME||'/root/.cache/ms-playwright/chromium-1187/chrome-linux/chrome',args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1280,height:1000}});
  page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:'+server.address().port);
  assert.equal(await page.getByLabel('订阅方式',{exact:true}).inputValue(),'web');
  assert.equal(await page.getByLabel('Telegram API ID',{exact:true}).count(),0);
  assert.equal(await page.getByLabel('手机号（含国家区号）',{exact:true}).count(),0);
  await page.getByRole('button',{name:'保存连接设置',exact:true}).click();
  await page.getByText('订阅设置已保存',{exact:true}).waitFor();
  await page.getByRole('tab',{name:'频道订阅',exact:true}).click();
  await page.getByLabel('频道（每行一个，最多30个）',{exact:true}).fill('https://t.me/s/example');
  await page.getByLabel('启用频道订阅',{exact:true}).check();
  await page.getByRole('button',{name:'保存订阅设置',exact:true}).click();
  const publicSaved=await page.evaluate(()=>window.calls.filter(c=>c.action==='telegram_settings').at(-1).settings);
  assert.equal(publicSaved.mode,'web');assert.equal(publicSaved.api_id,0);assert.equal(publicSaved.api_hash,'');
  assert.deepEqual(publicSaved.channels,['https://t.me/s/example']);
  assert.equal(await page.evaluate(()=>window.calls.filter(c=>['telegram_send_code','telegram_sign_in'].includes(c.action)).length),0);
  await page.getByRole('tab',{name:'连接设置',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'web mode viewport overflow');
  await page.screenshot({path:'/tmp/p115tool-telegram-web-mobile.png',fullPage:true});
  await page.setViewportSize({width:1280,height:1000});
  await page.getByLabel('订阅方式',{exact:true}).selectOption('account');
  await page.getByLabel('Telegram API ID',{exact:true}).fill('12345');
  await page.getByLabel('Telegram API Hash',{exact:true}).fill('a'.repeat(32));
  assert.equal(await page.getByLabel('频道（每行一个，最多30个）',{exact:true}).count(),0);
  await page.getByRole('tab',{name:'频道订阅',exact:true}).click();
  await page.getByLabel('频道（每行一个，最多30个）',{exact:true}).fill('@example\n-1001234567890');
  await page.getByLabel('启用频道订阅',{exact:true}).check();
  await page.getByRole('button',{name:'保存订阅设置',exact:true}).click();
  await page.getByText('订阅设置已保存',{exact:true}).waitFor();
  const saved=await page.evaluate(()=>window.calls.filter(c=>c.action==='telegram_settings').at(-1).settings);
  assert.deepEqual(saved.channels,['@example','-1001234567890']);assert.equal(saved.enabled,true);
  assert.equal(saved.api_id,'12345');assert.equal(saved.api_hash,'a'.repeat(32));
  await page.getByRole('tab',{name:'连接设置',exact:true}).click();
  assert.equal(await page.getByLabel('Telegram API Hash',{exact:true}).inputValue(),'');
  await page.getByLabel('手机号（含国家区号）',{exact:true}).fill('+8613800000000');
  await page.getByRole('button',{name:'发送验证码',exact:true}).click();
  await page.getByText('验证码已发送，请在 Telegram 或短信中查看',{exact:true}).waitFor();
  await page.getByLabel('验证码',{exact:true}).fill('12345');
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByText('此账号开启了二步验证，请填写密码后登录',{exact:true}).waitFor();
  await page.getByLabel('二步验证密码（提示需要时填写）',{exact:true}).fill('test-only-password');
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByText('登录成功',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('验证码',{exact:true}).inputValue(),'');
  assert.equal(await page.getByLabel('二步验证密码（提示需要时填写）',{exact:true}).inputValue(),'');
  await page.getByRole('tab',{name:'导入记录',exact:true}).click();
  assert.equal(await page.getByLabel('Telegram API ID',{exact:true}).count(),0);
  await page.getByRole('button',{name:'重试',exact:true}).click();
  await page.getByText('已安排重新导入',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>window.wrappers.Config.name),'P115ConfigWithTelegram');
  assert.equal(await page.evaluate(()=>window.wrappers.Page.name),'P115PageWithTelegram');
  await page.getByRole('tab',{name:'连接设置',exact:true}).focus();
  await page.keyboard.press('ArrowRight');
  await page.getByRole('tab',{name:'频道订阅',exact:true}).waitFor({state:'visible'});
  assert.equal(await page.getByRole('tab',{name:'频道订阅',exact:true}).getAttribute('aria-selected'),'true');
  assert.equal(await page.getByRole('tab',{name:'频道订阅',exact:true}).evaluate(el=>el===document.activeElement),true);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'mobile viewport overflow');
  await page.screenshot({path:process.env.P115_UI_TEST_SCREENSHOT||'/tmp/p115tool-telegram-mobile.png',fullPage:true});
  // Use actual baseline components and Vuetify, rather than a navigation stub.
  await page.setViewportSize({width:1280,height:1000});
  await page.goto('http://127.0.0.1:'+server.address().port+'/?view=config');
  const configNav=page.getByRole('tablist',{name:'配置板块',exact:true});
  await configNav.getByRole('tab',{name:'Telegram',exact:true}).waitFor();
  assert.equal(await page.locator('.p115-tg').count(),0,'Telegram must not be appended below ordinary settings');
  await page.getByLabel('115 Cookie',{exact:true}).fill('unsaved-cookie');
  await configNav.getByRole('tab',{name:'Telegram',exact:true}).click();
  await page.getByLabel('订阅方式',{exact:true}).selectOption('account');
  await page.getByLabel('Telegram API ID',{exact:true}).fill('54321');
  assert.equal(await page.getByLabel('115 Cookie',{exact:true}).count(),0);
  assert.equal(await page.locator('.p115-savebar').count(),0,'ordinary save bar must not appear in Telegram');
  await configNav.getByRole('tab',{name:'通用连接',exact:true}).click();
  assert.equal(await page.getByLabel('115 Cookie',{exact:true}).inputValue(),'unsaved-cookie');
  assert.equal(await page.locator('.p115-tg').isVisible(),false);
  const before=await page.evaluate(()=>window.calls.filter(c=>c.kind==='telegram').length);
  await page.waitForTimeout(5100);
  assert.equal(await page.evaluate(()=>window.calls.filter(c=>c.kind==='telegram').length),before,'hidden Telegram must pause status polling');
  await configNav.getByRole('tab',{name:'Telegram',exact:true}).click();
  assert.equal(await page.getByLabel('Telegram API ID',{exact:true}).inputValue(),'54321');
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'configuration viewport overflow');
  await page.waitForTimeout(300);
  const telegramTab=await configNav.getByRole('tab',{name:'Telegram',exact:true}).boundingBox();
  assert.ok(telegramTab.x>=0&&telegramTab.x+telegramTab.width<=390,'mobile Telegram tab must be fully visible: '+JSON.stringify(telegramTab));
  const firstTab=await configNav.getByRole('tab',{name:'整理',exact:true}).boundingBox();
  assert.ok(firstTab.x>=0,'all five mobile tabs should fit: '+JSON.stringify(firstTab));
  await page.screenshot({path:'/tmp/p115tool-telegram-config-mobile.png',fullPage:true});
  await page.getByRole('button',{name:'关闭',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>window.events),['close']);
  await page.goto('http://127.0.0.1:'+server.address().port+'/?view=page');
  const pageNav=page.getByRole('tablist',{name:'功能板块',exact:true});
  await pageNav.getByRole('tab',{name:'Telegram',exact:true}).click();
  await page.getByLabel('订阅方式',{exact:true}).waitFor();
  assert.equal(await page.getByLabel('Telegram API ID',{exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'整理设置',exact:true}).count(),0);
  await pageNav.getByRole('tab',{name:'整理',exact:true}).click();
  await page.getByRole('button',{name:'整理设置',exact:true}).click();
  assert.deepEqual(await page.evaluate(()=>window.events),['switch']);
  assert.deepEqual(errors,[]);
  console.log('PASS: default public web mode without API/login, mode switching, account login compatibility, real Vue/Vuetify navigation, drafts, hidden polling, host events, save/retry and 390px layouts');
} finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
