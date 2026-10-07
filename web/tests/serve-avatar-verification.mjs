import { build } from 'esbuild';
import { createReadStream, createWriteStream } from 'node:fs';
import { cp, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { dirname, extname, resolve, sep } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const projectRoot = resolve(webRoot, '..');
const verificationRoot = resolve(projectRoot, '.tools/live2d-verification');
const publicRoot = resolve(verificationRoot, 'public');
const resultsRoot = resolve(verificationRoot, 'results');
const port = 3001;
const origin = 'http://127.0.0.1:' + port;
const evidenceNames = new Set(['report.json', 'stability.json', 'recording.webm']);

await mkdir(publicRoot, { recursive: true });
await mkdir(resultsRoot, { recursive: true });
await cp(resolve(webRoot, 'public/avatar'), resolve(publicRoot, 'avatar'), { recursive: true });
await cp(resolve(verificationRoot, 'utterances'), resolve(publicRoot, 'utterances'), {
  recursive: true,
});
await build({
  absWorkingDir: webRoot,
  entryPoints: ['tests/browser-avatar-harness.ts'],
  outdir: publicRoot,
  entryNames: 'harness',
  chunkNames: 'chunks/[name]-[hash]',
  bundle: true,
  splitting: true,
  format: 'esm',
  platform: 'browser',
  target: 'chrome130',
  sourcemap: true,
  tsconfig: resolve(webRoot, 'tsconfig.json'),
});

const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>小芽 Live2D 合成音频验收</title><style>body{margin:0;background:#f5f7f3;color:#29443b;font:16px system-ui}main{max-width:1100px;margin:auto;padding:28px}.layout{display:grid;grid-template-columns:480px 1fr;gap:24px}canvas{width:480px;height:540px;border-radius:28px;background:#eaf3ec}button{border:0;border-radius:14px;padding:14px 20px;background:#296c54;color:white;margin:4px;font:inherit}button:disabled{opacity:.5}p{line-height:1.7}pre{white-space:pre-wrap;font-size:13px;background:white;padding:20px;border-radius:20px;max-height:440px;overflow:auto}#status{min-height:50px}h1{font-size:26px}@media(max-width:800px){.layout{grid-template-columns:1fr}canvas{width:100%;height:440px}}</style></head><body><main><h1>小芽 Live2D 合成音频验收</h1><p>正式 Cubism 模型、MotionSync CRI 和生产音频桥接。输入为私有 CosyVoice 生成的 30 条固定 WAV。<br>测量使用 AudioContext 软件输出时钟，不能替代物理扬声器回录、真人麦克风或逐字嘴型人工观看。</p><div class="layout"><canvas id="avatar" width="960" height="1080"></canvas><div><p id="status" role="status">正在加载正式人物模型…</p><button id="run" disabled>运行 30 条口型验收</button><button id="stability" disabled>开始 30 分钟稳定性</button><button id="cancel" disabled>取消当前验收</button><button id="save" disabled>保存证据</button><p>口型验收自动保存带声音 WebM，并执行 20 次音频连接与资源回收。稳定性验收每条语音后保存进度，取消会保留部分结果。保持本页可见可获得真实前台 FPS。</p><pre id="summary">尚未开始测量。</pre></div></div></main><script type="module" src="/harness.js"></script></body></html>`;
await writeFile(resolve(publicRoot, 'index.html'), html, 'utf8');

/** 路径始终解析到生成的测试目录，编码后的上级路径也不能读出工作区配置或密钥。 */
function safeFile(pathname) {
  const file = resolve(publicRoot, '.' + decodeURIComponent(pathname));
  if (file !== publicRoot && !file.startsWith(publicRoot + sep)) throw new Error('无效资源路径');
  return file;
}

/** 仅接收本机同源固定证据名称；录像流式落盘，长文件不堆积在 Node 内存。 */
async function handle(request, response) {
  try {
    if (request.headers.host !== '127.0.0.1:' + port) {
      response.writeHead(403).end();
      return;
    }
    const url = new URL(request.url ?? '/', origin);
    if (request.method === 'POST' && url.pathname.startsWith('/evidence/')) {
      const name = url.pathname.slice('/evidence/'.length);
      if (request.headers.origin !== origin || !evidenceNames.has(name)) {
        response.writeHead(403).end();
        return;
      }
      const length = Number(request.headers['content-length']);
      if (!Number.isFinite(length) || length > 100 * 1024 * 1024) {
        response.writeHead(413).end();
        return;
      }
      await pipeline(request, createWriteStream(resolve(resultsRoot, name)));
      response.writeHead(200, { 'Content-Type': 'application/json' }).end('{"saved":true}');
      return;
    }
    if (request.method !== 'GET') {
      response.writeHead(405).end();
      return;
    }
    const file = safeFile(url.pathname === '/' ? '/index.html' : url.pathname);
    if (!(await stat(file)).isFile()) throw new Error('资源不存在');
    const type =
      {
        '.html': 'text/html; charset=utf-8',
        '.js': 'text/javascript; charset=utf-8',
        '.json': 'application/json',
        '.png': 'image/png',
        '.wav': 'audio/wav',
        '.wasm': 'application/wasm',
      }[extname(file)] ?? 'application/octet-stream';
    response.writeHead(200, { 'Content-Type': type, 'Cache-Control': 'no-store' });
    await pipeline(createReadStream(file), response);
  } catch {
    if (!response.headersSent) response.writeHead(404);
    response.end();
  }
}

const manifest = JSON.parse(
  await readFile(resolve(publicRoot, 'utterances/manifest.json'), 'utf8')
);
if (!manifest.complete || manifest.utterances.length !== 30)
  throw new Error('30 条固定合成语音尚未完整生成');
const server = createServer((request, response) => void handle(request, response));
server.listen(port, '127.0.0.1', () => console.log('Live2D verification: ' + origin));
