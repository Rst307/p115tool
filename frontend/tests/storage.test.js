import test from 'node:test'
import assert from 'node:assert/strict'
import {storageTypes,virtualizationPayload,storageActionMessage,storageErrorMessage,recycleCheckpointMessage,conversionResultMessage,canSubmitShare,recycleEmptyMessage} from '../src/storage.js'

test('scheduled whole-bin deletion shows unknown and verified outcomes without private errors',()=>{
  assert.match(recycleEmptyMessage({stage:'UNKNOWN'}),/暂停.*只读核实/)
  assert.match(recycleEmptyMessage({stage:'VERIFIED'}),/其余条目留待下次/)
  assert.match(recycleEmptyMessage({stage:'DONE'}),/新增条目留待下次/)
  assert.match(recycleEmptyMessage({stage:'BLOCKED'}),/未清空/)
  assert.doesNotMatch(recycleEmptyMessage({stage:'PRIVATE',error:'PRIVATE'}),/PRIVATE/)
  assert.match(storageErrorMessage('RECYCLE_EMPTY_UNKNOWN'),/不会自动重试删除/)
})

test('share import remains available during other work but protects attachment and capacity',()=>{
  assert.equal(canSubmitShare({working:false,blocked:true,pending:3,limit:100}),true)
  assert.equal(canSubmitShare({working:true,blocked:false}),false)
  assert.equal(canSubmitShare({working:false,blocked:true,attaching:true}),false)
  assert.equal(canSubmitShare({working:false,blocked:false,pending:100,limit:100}),false)
  assert.match(storageActionMessage({state:'QUEUED',task_id:'test',position:3}),/第 3 项.*继续添加/)
  assert.match(storageActionMessage({state:'ALREADY_QUEUED',detail:'PRIVATE'}),/无需重复添加/)
  assert.doesNotMatch(storageActionMessage({state:'QUEUE_FULL',detail:'PRIVATE'}),/PRIVATE/)
  assert.match(storageErrorMessage('SHARE_VIDEO_LIMIT'),/1万个/)
})

test('missing directory explains the configuration step instead of claiming task submission',()=>{
  const message=storageActionMessage({state:'CONFIG_REQUIRED',field:'temp_cid',detail:'PRIVATE_TEST_RESPONSE'})
  assert.match(message,/存储设置/)
  assert.match(message,/临时转存目录并保存/)
  assert.doesNotMatch(message,/PRIVATE|任务已提交/)
  assert.match(storageActionMessage({state:'RUNNING'}),/任务已提交/)
  assert.doesNotMatch(storageActionMessage({state:'UNKNOWN',detail:'PRIVATE'}),/PRIVATE|任务已提交/)
  assert.match(storageActionMessage({state:'RECORDS_DELETED',count:2}),/记录已从列表移除/)
  assert.doesNotMatch(storageActionMessage({state:'RECORDS_DELETED'}),/任务已提交/)
})
test('only actual and virtual are exposed as storage types',()=>{
  assert.deepEqual(Object.keys(storageTypes),['ACTUAL','VIRTUAL'])
})
test('source deletion requires explicit consent and selected resources',()=>{
  for(const consent of [false,undefined,'true'])assert.throws(()=>virtualizationPayload(['1'],consent))
  assert.throws(()=>virtualizationPayload([],true))
  const ids=['1'];const payload=virtualizationPayload(ids,true);ids.push('2')
  assert.deepEqual(payload,{action:'virtualize',ids:['1'],delete_source:true})
})

test('all actual conversion uses server scope and explicit consent',()=>{
  for(const consent of [false,undefined,'true'])assert.throws(()=>virtualizationPayload([],consent,true))
  assert.deepEqual(virtualizationPayload([],true,true),{action:'virtualize_all',delete_source:true})
})

test('conversion purge is scoped to this request and key prerequisite is actionable',()=>{
  assert.deepEqual(virtualizationPayload(['1'],true,false,true),{action:'virtualize',ids:['1'],delete_source:true,purge_source:true})
  assert.deepEqual(virtualizationPayload([],true,true,true),{action:'virtualize_all',delete_source:true,purge_source:true})
  assert.throws(()=>virtualizationPayload(['1'],true,false,'true'))
  assert.match(storageActionMessage({state:'RECYCLE_KEY_REQUIRED',detail:'PRIVATE'}),/六位安全密钥/)
  assert.doesNotMatch(storageActionMessage({state:'RECYCLE_KEY_REQUIRED',detail:'PRIVATE'}),/PRIVATE|任务已提交/)
})

test('share failure explains source retention, read recheck and stopped unknown writes',()=>{
  assert.match(storageErrorMessage('SHARE_REJECTED'),/跳过，源文件保留/)
  assert.match(storageErrorMessage('SHARE_CHECK'),/复用已创建分享/)
  assert.match(storageErrorMessage('SHARE_CREATE_UNKNOWN'),/核实并接入/)
  assert.match(storageErrorMessage('RECYCLE_PURGE_BLOCKED'),/不会自动重试/)
  assert.doesNotMatch(storageErrorMessage('PRIVATE'),/PRIVATE/)
})

test('recycle checkpoint distinguishes unsubmitted purge and recorded target without exposing raw text',()=>{
  assert.match(recycleCheckpointMessage({target_recorded:false,reason:'LEGACY_CHECKPOINT'}),/尚未提交.*没有再次调用/)
  assert.match(recycleCheckpointMessage({target_recorded:true,reason:'RECYCLE_UPSTREAM_ERROR'}),/执行结果需核实.*接口失败/)
  assert.match(recycleCheckpointMessage({target_recorded:false,reason:'RECYCLE_IDENTITY_NOT_UNIQUE'}),/唯一回收站条目/)
  assert.doesNotMatch(recycleCheckpointMessage({target_recorded:true,reason:'PRIVATE_TEST_RESPONSE'}),/PRIVATE_TEST_RESPONSE/)
})

test('missing recycle identity explains the non-destructive recovery choice',()=>{
  const message=storageErrorMessage('RECYCLE_IDENTITY_UNSUPPORTED')
  assert.match(message,/源文件保留/)
  assert.match(message,/关闭全局及本次永久删除/)
  assert.match(message,/保留在回收站/)
})

test('conversion failure shows safe interface and step while preserving unknown-write warning',()=>{
  const message=conversionResultMessage({error:'SHARE_CREATE_UNKNOWN',reason:'SESSION_REJECTED',operation:'share_send',phase:'SHARE_CREATING'})
  assert.match(message,/结果未知/)
  assert.match(message,/完整Cookie/)
  assert.match(message,/接口：share_send/)
  assert.match(message,/失败步骤：创建分享/)
  assert.doesNotMatch(conversionResultMessage({error:'PRIVATE',reason:'PRIVATE',operation:'PRIVATE',phase:'PRIVATE'}),/PRIVATE/)
  assert.match(storageErrorMessage('SOURCE_MISSING'),/重新扫描/)
  assert.match(storageErrorMessage('STRM_PERMISSION_DENIED'),/权限/)
})
