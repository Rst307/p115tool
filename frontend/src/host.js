export function unwrap(response) {
  let value = response
  if (value && typeof value.status === 'number' && Object.hasOwn(value, 'data')) value = value.data
  if (value && Object.keys(value).length === 3 && typeof value.success === 'boolean' &&
      typeof value.message === 'string' && Object.hasOwn(value, 'data')) {
    if (!value.success) throw new Error('宿主请求未成功')
    return value.data
  }
  return value
}
class NativeRequestError extends Error {
  constructor(status) {
    super('插件接口请求未完成')
    this.status = status
  }
}
export function requestErrorMessage(error, fallback) {
  if (!(error instanceof NativeRequestError)) return fallback
  const messages = {
    HOST_API: '宿主未提供请求接口，请在MoviePilot插件页面内打开。',
    401: 'MoviePilot登录已失效（HTTP 401），请重新登录后刷新。',
    403: '需要已激活的MoviePilot管理员权限（HTTP 403），请检查当前登录账号。',
    404: '插件接口未注册（HTTP 404）。请更新插件并重新加载；若仍失败，请检查宿主插件加载日志及反向代理路径。',
    422: '请求参数未通过校验（HTTP 422），请检查筛选条件或配置字段。',
    500: '插件接口执行失败（HTTP 500），请检查宿主中的插件状态及脱敏日志。',
    503: '插件服务不可用（HTTP 503），请检查是否启用及初始化是否成功。',
  }
  return messages[error.status] || fallback
}
export async function post(api, route, payload) {
  if (!api || typeof api.post !== 'function') throw new NativeRequestError('HOST_API')
  try {
    return unwrap(await api.post(`plugin/P115Tool/native/${route}`, payload))
  } catch (error) {
    // Never propagate response bodies, request headers, URLs or exception text.
    const status = error?.response?.status ?? error?.status
    throw new NativeRequestError(Number.isInteger(status) ? status : null)
  }
}
export async function saveConfiguration(api, config, save) {
  try {
    await post(api, 'validate', {config})
  } catch (error) {
    // A missing plugin route must not lock users out of host-managed settings.
    // Host save remains administrator-authenticated; init_plugin validates the
    // full configuration before starting or replacing the running service.
    if (!(error instanceof NativeRequestError) || error.status !== 404) throw error
  }
  save(config)
}
export const time = value => value ? new Date(value * 1000).toLocaleString() : '未记录'
export const bytes = value => `${(Number(value || 0) / 1024 ** 3).toFixed(2)} GiB`
