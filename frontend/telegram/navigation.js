import {importShared} from './__federation_fn_import-E6wRZccp.js';
import Telegram from './telegram-panel.js';
const {h, ref, cloneVNode} = await importShared('vue');

// Extend the baseline's existing navigation without changing compiled assets.
// Calling its setup in this component preserves its form state and host events.
export function withTelegram(Base, name) {
  return {
    ...Base, name, __name:name,
    setup(props, context) {
      const active=ref(false), visited=ref(false);
      const render=Base.setup(props,context);
      return (...args)=>{
        const original=render(...args);
        const children=original.children;
        const index=children.findIndex(node=>['配置板块','功能板块'].includes(node?.props?.['aria-label']));
        if(index<0) return original;
        const nav=children[index], slots=nav.children;
        const onSelect=nav.props['onUpdate:modelValue'];
        const nextNav=cloneVNode(nav,{
          modelValue:active.value?'telegram':nav.props.modelValue,
          class:'p115-tg-navigation',grow:true,
        });
        // Replace the handler: cloneVNode merges event listeners by default.
        nextNav.props['onUpdate:modelValue']=value=>{
          active.value=value==='telegram';
          if(active.value) visited.value=true;
          else if(Array.isArray(onSelect)) onSelect.forEach(fn=>fn(value));
          else onSelect?.(value);
        };
        nextNav.children={...slots,default:()=>{
          const tabs=slots.default();
          return [...tabs,h(tabs[0].type,{value:'telegram',key:'telegram'},{default:()=> 'Telegram'})];
        }};
        const result=cloneVNode(original);
        result.dynamicChildren=null;
        result.patchFlag=0;
        result.children=children.flatMap((node,i)=>{
          if(i===index) return [nextNav];
          if(!active.value) return [node];
          if(i===0) {
            const header=cloneVNode(node);
            header.dynamicChildren=null;header.patchFlag=0;
            header.children=[h('div',[
              h('div',{class:'p115-eyebrow'},'115 工具箱 · Telegram'),
              h('h2','Telegram 频道订阅'),
              h('p','管理账号连接、频道订阅和虚拟资源导入。'),
            ]),h('button',{type:'button',class:'p115-tg-button',onClick:()=>context.emit('close')},'关闭')];
            return [header];
          }
          return [];
        });
        if(visited.value) result.children.push(h('div',{
          key:'telegram-content',hidden:!active.value,class:'p115-tg-outlet',
        },[h(Telegram,{api:props.api,active:active.value})]));
        return result;
      };
    },
  };
}
