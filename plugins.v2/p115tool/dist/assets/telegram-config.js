import {importShared} from './__federation_fn_import-E6wRZccp.js';
import Base from './__federation_expose_Config-BdKL4bEl.js';
import Telegram from './telegram-panel.js';
const {h, Fragment} = await importShared('vue');
export default {
  name:'P115ConfigWithTelegram', inheritAttrs:false,
  props:{initialConfig:{type:Object,default:()=>({})},api:Object},
  setup(props,{attrs}) {return ()=>h(Fragment,[h(Base,{...attrs,...props}),h(Telegram,{api:props.api})]);},
};
