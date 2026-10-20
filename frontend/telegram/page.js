import {importShared} from './__federation_fn_import-E6wRZccp.js';
import Base from './__federation_expose_Page-EoLwh5n3.js';
import Telegram from './telegram-panel.js';
const {h, Fragment} = await importShared('vue');
export default {
  name:'P115PageWithTelegram', inheritAttrs:false, props:{api:Object},
  setup(props,{attrs}) {return ()=>h(Fragment,[h(Base,{...attrs,...props}),h(Telegram,{api:props.api})]);},
};
