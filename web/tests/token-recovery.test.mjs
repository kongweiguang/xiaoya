import { TokenSource } from 'livekit-client';
import * as server from 'livekit-server-sdk';
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { production } from './helpers/production.mjs';

const keys = [
  'LIVEKIT_URL',
  'LIVEKIT_API_KEY',
  'LIVEKIT_API_SECRET',
  'AGENT_NAME',
  'TOKEN_GATEWAY_ORIGIN',
  'TOKEN_GATEWAY_KEY',
  'NODE_ENV',
];
const original = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
/** 恢复配置的存在性及原值，缺失配置场景不能继承前一用例的有效凭据。 */
afterEach(() => {
  for (const key of keys)
    if (original[key] === undefined) delete process.env[key];
    else process.env[key] = original[key];
});

/** 测试仅签发内存凭据，环境固定为 production，以检验不存在演示绕过或环境拒绝。 */
async function route() {
  Object.assign(process.env, {
    LIVEKIT_URL: 'ws://172.20.0.2:7880',
    LIVEKIT_API_KEY: 'test-key',
    LIVEKIT_API_SECRET: 'synthetic-secret-never-used-on-services',
    AGENT_NAME: 'xiaoya',
    NODE_ENV: 'production',
  });
  delete process.env.TOKEN_GATEWAY_ORIGIN;
  delete process.env.TOKEN_GATEWAY_KEY;
  return production('app/api/token/route.ts', {
    'livekit-server-sdk': server,
    'next/server': { NextResponse: Response },
  });
}

/** 保留浏览器同源请求头，正文与网络来源变化都由实际 route 判断。 */
function request(body = {}, headers = {}, url = 'http://127.0.0.1:3000/api/token') {
  const parsed = new URL(url);
  return new Request(url, {
    method: 'POST',
    body: JSON.stringify(body),
    headers: {
      host: parsed.host,
      origin: parsed.origin,
      'sec-fetch-site': 'same-origin',
      'content-type': 'application/json',
      ...headers,
    },
  });
}

/** 真实 SDK 签名验证授权最小化、派发、TTL 和恢复窗口，HTTP 成功本身不算契约验收。 */
test('生产环境回环空请求签发服务端固定房间，只有麦克风与数据权限', async () => {
  const { POST } = await route();
  const first = await POST(request());
  const second = await POST(request());
  assert.equal(first.status, 201);
  assert.equal(first.headers.get('cache-control'), 'no-store');
  const data = await first.json();
  assert.deepEqual(Object.keys(data).sort(), [
    'participant_name',
    'participant_token',
    'room_name',
    'server_url',
  ]);
  assert.notEqual((await second.json()).room_name, data.room_name);
  const claims = await new server.TokenVerifier(
    process.env.LIVEKIT_API_KEY,
    process.env.LIVEKIT_API_SECRET
  ).verify(data.participant_token);
  assert.equal(claims.video.room, data.room_name);
  assert.equal(claims.video.roomJoin, true);
  assert.equal(claims.video.canPublishData, true);
  assert.equal(claims.video.canSubscribe, true);
  assert.deepEqual(claims.video.canPublishSources, ['microphone']);
  for (const name of ['roomAdmin', 'roomCreate', 'roomList', 'agent', 'canUpdateOwnMetadata'])
    assert.ok(!claims.video[name]);
  assert.equal(claims.exp - claims.nbf, 900);
  assert.equal(claims.roomConfig.departureTimeout, 35);
  assert.equal(claims.roomConfig.metadata, JSON.stringify({ xiaoya: { v: 1, client: 'web' } }));
  assert.equal(claims.roomConfig.agents[0].agentName, 'xiaoya');
});

/** 当前浏览器 SDK 必须实际解析唯一 snake_case 201 契约，不能只断言服务端字段名称。 */
test('当前 TokenSource.endpoint 空请求读取标准 201 响应', async () => {
  const { POST } = await route();
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(options);
    return POST(request(JSON.parse(options.body)));
  };
  try {
    const details = await TokenSource.endpoint('http://127.0.0.1:3000/api/token').fetch({});
    assert.deepEqual(JSON.parse(requests[0].body), {});
    assert.equal(requests[0].method, 'POST');
    assert.equal(details.serverUrl, 'ws://172.20.0.2:7880');
    assert.ok(details.participantToken);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

/** 不支持的字段必须拒绝，不覆盖后继续签名；旧 room_config 也属于不兼容输入。 */
test('只允许空 JSON 对象，旧配置和身份／权限输入一律拒绝', async () => {
  const { POST } = await route();
  for (const body of [
    null,
    [],
    '',
    1,
    { room_config: { agents: [{ agent_name: 'other' }] } },
    { identity: 'admin' },
    { agentName: 'other' },
    { grant: { roomAdmin: true } },
  ]) {
    const response = await POST(request(body));
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, 'invalid_request');
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
  const malformed = request();
  malformed.json = async () => {
    throw new Error('bad json');
  };
  assert.equal((await POST(malformed)).status, 400);
});

/** CORS 不等于权限，恶意 Origin、跨站、缺少来源和 Host 不符均不能得到签名令牌。 */
test('本机签发要求真实回环同源请求', async () => {
  const { POST } = await route();
  for (const candidate of [
    request({}, { origin: 'https://attacker.example' }),
    request({}, { origin: '' }),
    request({}, { host: 'attacker.example' }),
    request({}, { 'sec-fetch-site': 'cross-site' }),
    request({}, {}, 'https://xiaoya.example/api/token'),
  ])
    assert.equal((await POST(candidate)).status, 403);
  assert.equal((await POST(request({}, {}, 'http://localhost:3000/api/token'))).status, 201);
  assert.equal((await POST(request({}, {}, 'http://[::1]:3000/api/token'))).status, 201);
  // Next 的内部 req.url 可以仍是 localhost，同源边界必须使用真实 HTTP Host 而非内部名称。
  assert.equal(
    (
      await POST(
        request(
          {},
          { host: '127.0.0.1:3000', origin: 'http://127.0.0.1:3000' },
          'http://localhost:3000/api/token'
        )
      )
    ).status,
    201
  );
});

/** 网关必须先完成认证并注入服务端密钥，配置 Origin 单独存在不能开放匿名公网。 */
test('远程签发同时要求网关来源和注入密钥', async () => {
  const { POST } = await route();
  process.env.TOKEN_GATEWAY_ORIGIN = 'https://xiaoya.example';
  /** 来源固定在真实远程边界，仅变化代理注入头以隔离认证条件。 */
  const remote = (headers = {}) => request({}, headers, 'https://xiaoya.example/api/token');
  assert.equal((await POST(remote())).status, 403);
  process.env.TOKEN_GATEWAY_KEY = 'test-gateway-key';
  assert.equal((await POST(remote())).status, 403);
  assert.equal((await POST(remote({ 'x-xiaoya-gateway-key': 'bad' }))).status, 403);
  assert.equal((await POST(remote({ 'x-xiaoya-gateway-key': 'test-gateway-key' }))).status, 201);
  assert.equal(
    (
      await POST(
        remote({ origin: 'https://other.example', 'x-xiaoya-gateway-key': 'test-gateway-key' })
      )
    ).status,
    403
  );
});

/** 错误不得泄漏地址与密钥；公共 OpenAI／LiveKit 域即使带末尾点也不能接收本机凭据。 */
test('配置缺失和非法服务地址返回安全且可重试的错误', async () => {
  const { POST } = await route();
  for (const key of ['LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET', 'AGENT_NAME']) {
    const value = process.env[key];
    delete process.env[key];
    const response = await POST(request());
    assert.equal(response.status, 503);
    assert.equal((await response.json()).error.code, 'service_unavailable');
    process.env[key] = value;
  }
  for (const url of [
    'https://private.example',
    'ws://user:secret@private.example',
    'ws://private.example:99999',
    'ws://private.example:0',
    'ws://private.example/#secret',
    'wss://private.example/?access_token=secret',
    'wss://livekit.cloud',
    'wss://demo.livekit.cloud.',
    'wss://demo.livekit.cloud',
    'wss://OPENAI.COM.',
    'wss://api.openai.com',
    'wss://api.openai.com.',
    'wss://gateway.openai.com',
    'wss://gateway.openai.com.',
  ]) {
    process.env.LIVEKIT_URL = url;
    const response = await POST(request());
    assert.equal(response.status, 503);
    const content = await response.text();
    assert.ok(!content.includes(url));
    assert.ok(!content.includes(process.env.LIVEKIT_API_SECRET));
  }
});
