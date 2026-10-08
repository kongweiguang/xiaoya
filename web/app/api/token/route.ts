import { NextResponse } from 'next/server';
import { AccessToken, RoomAgentDispatch, RoomConfiguration, TrackSource } from 'livekit-server-sdk';
import { timingSafeEqual } from 'node:crypto';

export const revalidate = 0;

/** 任何返回都禁止缓存；错误只含稳定代码与安全提示，不回传密钥或 SDK 异常正文。 */
function failure(code: string, message: string, status: number): NextResponse {
  return NextResponse.json(
    { error: { code, message } },
    { status, headers: { 'Cache-Control': 'no-store' } }
  );
}

/** 网关密钥由已认证反向代理注入，浏览器不持有它；长度不匹配也不会进入时序敏感比较。 */
function trustedGateway(req: Request, origin: string): boolean {
  const gatewayOrigin = process.env.TOKEN_GATEWAY_ORIGIN;
  const expected = process.env.TOKEN_GATEWAY_KEY;
  const provided = req.headers.get('x-xiaoya-gateway-key');
  if (!gatewayOrigin || !expected || !provided || origin !== gatewayOrigin) return false;
  const left = Buffer.from(expected);
  const right = Buffer.from(provided);
  return left.length === right.length && timingSafeEqual(left, right);
}

/** 回环访问要求同源；远程请求只有经过显式配置且已认证的网关才有签发权限。 */
function allowedRequest(req: Request): boolean {
  const origin = req.headers.get('origin');
  const host = req.headers.get('host');
  if (!origin || !host) return false;
  const site = req.headers.get('sec-fetch-site');
  if (site && site !== 'same-origin') return false;
  try {
    // Next 可把 req.url 规范为内部监听主机；浏览器实际 HTTP Host 才是同源校验的外部边界。
    const authority = new URL(`${new URL(req.url).protocol}//${host}`);
    if (
      authority.username ||
      authority.password ||
      authority.pathname !== '/' ||
      authority.search ||
      authority.hash
    )
      return false;
    if (origin !== authority.origin) return false;
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(authority.hostname.toLowerCase());
    return loopback || trustedGateway(req, origin);
  } catch {
    return false;
  }
}

/** 服务端独占签名与私有地址边界；空对象及公共域拒绝避免浏览器提权或错发临时凭据。 */
export async function POST(req: Request): Promise<NextResponse> {
  if (!allowedRequest(req)) return failure('forbidden', '当前访问方式未获授权。', 403);
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return failure('invalid_request', '请求必须是空 JSON 对象。', 400);
  }
  if (!body || typeof body !== 'object' || Array.isArray(body) || Object.keys(body).length !== 0)
    return failure('invalid_request', '请求必须是空 JSON 对象。', 400);

  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  const serverUrl = process.env.LIVEKIT_URL;
  const agentName = process.env.AGENT_NAME;
  if (!apiKey || !apiSecret || !serverUrl || !agentName)
    return failure('service_unavailable', '聊天服务尚未配置完成。', 503);
  try {
    const service = new URL(serverUrl);
    const hostname = service.hostname.toLowerCase().replace(/\.$/, '');
    if (
      !['ws:', 'wss:'].includes(service.protocol) ||
      !hostname ||
      service.username ||
      service.password ||
      service.hash ||
      serverUrl.includes('?') ||
      service.port === '0' ||
      hostname === 'livekit.cloud' ||
      hostname.endsWith('.livekit.cloud') ||
      hostname === 'openai.com' ||
      hostname.endsWith('.openai.com')
    )
      throw new Error('Invalid private endpoint');
    const participantName = 'user';
    const roomName = `xiaoya_room_${crypto.randomUUID()}`;
    const token = new AccessToken(apiKey, apiSecret, {
      identity: `xiaoya_user_${crypto.randomUUID()}`,
      name: participantName,
      ttl: '15m',
    });
    token.addGrant({
      room: roomName,
      roomJoin: true,
      canPublish: true,
      canPublishSources: [TrackSource.MICROPHONE],
      canPublishData: true,
      canSubscribe: true,
    });
    token.roomConfig = new RoomConfiguration({
      departureTimeout: 35,
      metadata: JSON.stringify({ xiaoya: { v: 1, client: 'web' } }),
      agents: [new RoomAgentDispatch({ agentName })],
    });
    const participantToken = await token.toJwt();
    return NextResponse.json(
      {
        server_url: serverUrl,
        room_name: roomName,
        participant_name: participantName,
        participant_token: participantToken,
      },
      { status: 201, headers: { 'Cache-Control': 'no-store' } }
    );
  } catch {
    return failure('service_unavailable', '聊天服务暂不可用，请稍后重试。', 503);
  }
}
