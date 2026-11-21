export const storageTypes = {ACTUAL:'实际存储',VIRTUAL:'虚拟存储'}
export const temporaryStorageHint = '请先打开“存储设置”，选择专用的115临时转存目录并保存，再播放虚拟资源。分享导入和实际转虚拟无需临时目录。'
export function canSubmitShare({working,blocked,attaching=false,pending=0,limit=100}) {
  return !working && (attaching ? !blocked : pending<limit)
}
export function storageActionMessage(result) {
  if(result?.state==='COOKIE_MODE_REQUIRED') return '此操作需要在通用连接中切换到 115 Cookie 模式。'
  if(result?.state==='RECORDS_DELETED') return '存储记录已从列表移除，网盘文件、分享和STRM保留，已有临时副本继续按原规则清理。'
  if(result?.state==='SOURCES_REQUIRED') return '请在STRM设置中选择整理后的媒体库源文件夹，再扫描实际资源。'
  if(result?.state==='CONFIG_REQUIRED') return temporaryStorageHint
  if(result?.state==='QUEUED') return result.task_id ? `分享已加入队列，当前等待顺序为第 ${result.position} 项，可继续添加其他分享。` : '操作已排队，扫描完成后核实文件并执行，请勿重复提交。'
  if(result?.state==='ALREADY_QUEUED') return '该分享正在处理或已在等待队列中，无需重复添加。'
  if(result?.state==='QUEUE_FULL') return '分享等待队列已满（100项），请等待部分任务完成后再添加。'
  if(result?.state==='START_FAILED') return '分享任务未能启动，请稍后重新添加。'
  if(result?.state==='BUSY') return '已有任务运行，请完成后再提交。'
  if(result?.state==='ATTACHED') return '分享已接入，可再次提交虚拟化继续验证。'
  if(result?.state==='RUNNING') return '任务已提交，状态每3秒更新。'
  if(result?.state==='RECYCLE_KEY_REQUIRED') return '请先在存储设置中填写115回收站六位安全密钥并保存，再执行本次永久删除。尚未创建分享或删除源文件。'
  return '提交状态未确认，请刷新并核实结果。'
}
export const storageStages = {
  READY:'可用', SHARE_CREATING:'创建分享中', SHARE_CREATE_UNKNOWN:'分享创建结果未知',
  SHARE_CHECK:'分享待验证', SOURCE_DELETING:'源文件删除中', SOURCE_DELETE_UNKNOWN:'源删除结果未知',
  SHARE_REJECTED:'115已拒绝分享，源文件保留',
  SHARE_UNAVAILABLE:'分享不可用，需核实', FOLDER_CREATING:'创建临时目录中',
  FOLDER_UNKNOWN:'临时目录结果未知', EMPTY:'需要时重新转存', RECEIVING:'转存中',
  RECEIVE_UNKNOWN:'转存待恢复（播放时自动核实）', RECEIVED:'等待核实副本', DELETING:'清理中', DELETE_UNKNOWN:'清理结果未知',
}
export function virtualizationPayload(ids, consent, all=false, purge=false) {
  if(consent!==true || typeof all!=='boolean' || (!all && (!Array.isArray(ids) || !ids.length))) throw new Error('请明确选择资源并确认删除源视频')
  if(typeof purge!=='boolean') throw new Error('请明确永久删除范围')
  const extra=purge?{purge_source:true}:{}
  if(all) return {action:'virtualize_all',delete_source:true,...extra}
  return {action:'virtualize',ids:[...ids],delete_source:true,...extra}
}
export const storagePhases = {
  SOURCE_CHECK:'读取并核实源文件',SOURCE_RECHECK:'删源前再次核实身份',STRM_WRITING:'核实或写入STRM',
  IMPORTING:'识别分享视频并生成STRM',STARTING:'准备任务',SCANNING:'扫描目录',REGISTERING:'登记视频与识别STRM路径',CHECKING:'核实文件及STRM',SHARE_CREATING:'创建分享',
  SHARE_RETAINING:'设置长期分享',SHARE_CHECK:'验证分享身份与播放',SHARE_WAITING:'等待分享处理后复查',
  RECYCLE_SNAPSHOT:'读取本次删除前的回收站',RECYCLE_RECHECK:'只读核实回收站检查点',SOURCE_DELETING:'删除网盘源文件',
  RECYCLE_EMPTY_SNAPSHOT:'记录整个回收站的条目快照',RECYCLE_EMPTY_PURGING:'分批清空回收站',
  POST_DELETE_CHECK:'删源后验证分享',RECYCLE_PURGING:'永久删除本次匹配的回收站条目',POST_PURGE_CHECK:'永久删除后验证分享',FINISHED:'已结束',
}
export const storageErrors = {
  RECYCLE_EMPTY_UNKNOWN:'定时清空回收站结果未知，后续清空已暂停。请只读核实；不会自动重试删除。',
  RECYCLE_EMPTY_BLOCKED:'定向永久删除有未核实检查点，本次未清空回收站。请先核实原检查点。',
  RECYCLE_EMPTY_FAILED:'回收站读取或安全检查失败，本次未提交新的永久删除。请检查115连接和安全密钥。',
  SHARE_VIDEO_LIMIT:'分享超过1万个视频，本次未导入。请拆分分享后重新添加。',
  SESSION_REJECTED:'115拒绝当前接口的会话校验，已停止后续处理。请重新登录115，更新完整Cookie后再核实失败资源；这不能证明源文件已丢失。',
  UPSTREAM_VERIFICATION_REQUIRED:'115要求验证，已停止后续处理。请先在115官方页面完成验证，再更新Cookie并核实失败资源。',
  SOURCE_MISSING:'115明确返回源文件不存在，已跳过该资源；请重新扫描媒体库，并核实是否已移走或删除。',
  SOURCE_READ_FAILED:'读取源文件详情失败，未提交本次删源。请检查115连接及当前Cookie。',
  SOURCE_INVALID:'源文件记录或ID不合法，未提交本次删源。请重新扫描媒体库。',
  SOURCE_IDENTITY_CHANGED:'源文件名称、大小、SHA1或父目录与记录不一致，未提交本次删源。请重新扫描并核实资源身份。',
  WRITE_CHECKPOINT_UNRESOLVED:'已有未核实的写操作检查点，请先只读核实资源，不能直接重复提交删除。',
  STRM_PERMISSION_DENIED:'STRM输出目录无写入权限，未提交本次删源。请检查MoviePilot目录挂载及运行用户权限。',
  STRM_DISK_FULL:'STRM输出磁盘空间不足，未提交本次删源。请释放空间后重新选择转换。',
  STRM_IO_FAILED:'STRM文件读写失败，未提交本次删源。请检查目录挂载及文件系统状态。',
  STRM_ENCODING_FAILED:'现有STRM编码无法读取，未提交本次删源。请核实原文件内容。',
  STRM_CONFLICT:'现有STRM内容冲突或被修改，未覆盖该文件，也未提交本次删源。请核实STRM内容和归属。',
  STRM_PATH_INVALID:'STRM输出路径不安全，未提交本次删源。请检查媒体路径及符号链接。',
  STRM_CHECKPOINT_UNRESOLVED:'STRM地址更新有未核实检查点，未提交本次删源。请核实输出文件。',
  SHARE_RETAIN_FAILED:'长期分享设置未确认，源文件保留。请检查115会话及分享状态。',
  SHARE_IDENTITY_NOT_UNIQUE:'分享中的文件身份无法唯一核实，请检查分享内容；不会继续提交本次删除。',
  SHARE_PLAYBACK_NOT_AVAILABLE:'分享单字节播放验证未通过，请核实115分享可用性。',
  SHARE_MEDIA_NOT_AVAILABLE:'115分享尚未提供视频，请等待处理完成后核实。',
  SHARE_REJECTED:'115拒绝创建分享；该文件已跳过，源文件保留。可检查网盘中的分享限制，修正后重新选择转换。',
  SHARE_CHECK:'分享暂未通过验证；源文件保留。批次末尾复查一次；仍失败时可等待115处理完成后重新选择转换，将复用已创建分享。',
  SHARE_UNAVAILABLE:'删源后分享不可用，已停止批次。分享映射与STRM保留，请立即核实115分享。',
  SHARE_CREATE_UNKNOWN:'分享创建结果未知，已停止批次。请核实并接入已创建的分享，避免重复创建。',
  SOURCE_DELETE_UNKNOWN:'源文件删除结果未知，已停止批次。请先只读核实结果，删除不会自动重试。',
  RECYCLE_PURGE_BLOCKED:'回收站永久删除未完成或结果未知，已停止后续删除。请核实网盘回收站及安全密钥；检查点不会自动重试。',
  RECYCLE_PRECHECK_FAILED:'回收站读取或删除前身份核实失败，源文件保留。请检查115连接及回收站状态，再重新选择转换。',
  RECYCLE_IDENTITY_UNSUPPORTED:'115回收站未提供原文件ID或SHA1，无法安全匹配永久删除目标；源文件保留。请关闭全局及本次永久删除后继续转换，删除的视频将保留在回收站。',
  VIRTUAL_ORGANIZE_FAILED:'部分视频未完成虚拟整理或STRM生成。请检查MoviePilot识别、命名模板、STRM权限及同名冲突，再重新导入。',
}
export function storageErrorMessage(code) {return storageErrors[code] || '请检查115连接、文件身份及STRM输出；结果未知时先核实。'}

export function recycleEmptyMessage(status) {
  if(status.error && storageErrors[status.error]) return storageErrors[status.error]
  return ({IDLE:'尚未执行。',PREPARED:'正在准备下一批。',PURGING:'正在永久删除并核实。',UNKNOWN:'结果未知，已暂停后续清空，请只读核实。',DONE:'本次快照已清空；执行中新增条目留待下次。',VERIFIED:'先前未知批次已核实删除；其余条目留待下次定时执行。',CANCELLED:'任务已中断，未继续提交后续批次。',BLOCKED:'已有未核实的定向永久删除，本次未清空。',FAILED:'读取或安全检查失败，请检查115连接与安全密钥。'})[status.stage] || '请刷新状态核实。'
}

export function conversionResultMessage(result) {
  let message=storageErrorMessage(result.error)
  if(result.reason && result.reason!==result.error && storageErrors[result.reason]) message+=' '+storageErrorMessage(result.reason)
  if(storagePhases[result.phase]) message+=' 失败步骤：'+storagePhases[result.phase]+'。'
  if(['fs_file','fs_files','share_send','share_update','share_snap','share_download_url','fs_delete','recyclebin_list','recyclebin_clean'].includes(result.operation)) message+=' 接口：'+result.operation+'。'
  return message
}

export function recycleCheckpointMessage(checkpoint) {
  const prefix=checkpoint.target_recorded?'已记录永久删除目标，执行结果需核实。':'永久删除尚未提交。'
  const reasons={
    LEGACY_CHECKPOINT:'旧检查点未保存具体原因；本次因它暂停，没有再次调用回收站接口。',
    RECYCLE_IDENTITY_NOT_UNIQUE:'未找到本次新增且身份完整匹配的唯一回收站条目。',
    RECYCLE_PURGE_UNCONFIRMED:'永久删除后仍看到目标条目，结果未确认。',
    RECYCLE_LIST_UNSUPPORTED:'回收站列表格式无法安全读取。',
    RECYCLE_ENTRY_UNSUPPORTED:'回收站条目格式无法安全读取。',
    RECYCLE_ENTRY_INVALID:'回收站条目ID缺失、非法或重复。',
    RECYCLE_LIST_INCOMPLETE:'回收站分页结果不完整。',
    RECYCLE_UPSTREAM_ERROR:'115接口失败；具体数字错误码见下方。',
    RECYCLE_SOURCE_PRESENT:'删除后源文件仍存在，停止永久删除。',
    RECYCLE_UNKNOWN:'没有可确定的失败原因，需核实。',
  }
  return prefix+(reasons[checkpoint.reason] || reasons.RECYCLE_UNKNOWN)
}
