import test from 'node:test'
import assert from 'node:assert/strict'
import { post, requestErrorMessage, saveConfiguration } from '../src/host.js'

test('404 native/data is identified without leaking host error contents or retrying', async () => {
  let calls = 0
  const api = {async post(path, payload) {
    calls++
    assert.equal(path, 'plugin/P115Tool/native/data')
    assert.deepEqual(payload, {kind: 'bootstrap'})
    throw {message: 'SECRET_COOKIE', response: {status: 404, data: 'SECRET_TOKEN'}}
  }}
  await assert.rejects(post(api, 'data', {kind: 'bootstrap'}), error => {
    const message = requestErrorMessage(error, '读取失败')
    assert.match(message, /接口未注册（HTTP 404）/)
    assert.doesNotMatch(message + error.message, /SECRET/)
    return true
  })
  assert.equal(calls, 1)
})

test('login, authorization, validation and server failures have distinct safe messages', async () => {
  for (const status of [401, 403, 422, 500, 503]) {
    await assert.rejects(post({post: async () => {
      throw {response: {status, data: 'SECRET'}}
    }}, 'data', {}), error => {
      assert.match(requestErrorMessage(error, '读取失败'), new RegExp(`HTTP ${status}`))
      return true
    })
  }
  await assert.rejects(post(null, 'data', {}), error => {
    assert.match(requestErrorMessage(error, '读取失败'), /宿主未提供请求接口/)
    return true
  })
  await assert.rejects(post({post: async () => { throw new Error('SECRET') }}, 'data', {}), error => {
    assert.equal(requestErrorMessage(error, '读取失败'), '读取失败')
    assert.doesNotMatch(error.message, /SECRET/)
    return true
  })
})

test('both host-intercepted data and Axios envelopes return bootstrap correctly', async () => {
  const data = {enabled: true, nonce: 'test', schema: []}
  for (const response of [data, {status: 200, data}, {success: true, message: '', data}]) {
    assert.deepEqual(await post({post: async () => response}, 'data', {kind: 'bootstrap'}), data)
  }
})

test('missing plugin validation route still permits host-managed start and stop', async () => {
  for (const enabled of [true, false]) {
    const config = {enabled, cookie: 'TEST_ONLY'}
    const saved = []
    await saveConfiguration({post: async () => { throw {response: {status: 404}} }}, config, value => saved.push(value))
    assert.deepEqual(saved, [config])
  }
})

test('host save is blocked for validation, authentication and unknown failures', async () => {
  for (const status of [401, 403, 422, 500, undefined]) {
    let saved = false
    await assert.rejects(saveConfiguration({post: async () => { throw {response: {status}} }}, {enabled: true}, () => {saved = true}))
    assert.equal(saved, false)
  }
  let saves = 0
  await saveConfiguration({post: async () => ({valid: true})}, {enabled: true}, () => saves++)
  assert.equal(saves, 1)
})
