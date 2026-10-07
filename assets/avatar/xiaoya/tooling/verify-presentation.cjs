const fs = require('node:fs');
const path = require('node:path');
const { build } = require('../../../../web/node_modules/esbuild');
const { chromium } = require('C:/Users/24052/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

/** 真实 Runtime/Core/WebGL 的定向验收；请求在浏览器内映射本地文件，不启动服务或连接模型供应商。 */
async function main() {
  const root = path.resolve(__dirname, '../../../..');
  if (!process.argv[2] || !process.argv[3])
    throw new Error('用法：node verify-presentation.cjs <模型目录> <验收输出目录>');
  const model = path.resolve(process.argv[2]);
  const output = path.resolve(process.argv[3]);
  fs.mkdirSync(output, { recursive: true });
  const entry = `
    import { Live2DRuntime } from './lib/avatar/live2d-runtime';
    import { CubismFramework } from './lib/avatar/vendor/cubism/src/live2dcubismframework';
    let delivery = null, lip = null;
    const frames = [];
    const runtime = await Live2DRuntime.load(document.querySelector('canvas'), {
      /** 测试 cue 与音频由场景显式设置，不连接会话或借口型推断情绪。 */
      readDelivery: () => delivery,
      /** 合成数值只用于表现回归，不代表真实房间语音已通过。 */
      readLip: () => lip,
      /** WebGL 故障立即使验收失败，不允许用静态后备掩盖。 */
      onFault: () => { throw new Error('WebGL preview failed'); },
      /** 本验收无自动重试，恢复事件必须明确报告。 */
      onRestore: () => { throw new Error('Unexpected context restoration'); },
      /** 有限帧记录用于证明口型独占与清理，不记录真实音频。 */
      onFrame: frame => { frames.push(frame); if (frames.length > 240) frames.shift(); },
    }, new AbortController().signal);
    window.preview = {
      /** 每个测试拥有新 id，已有 cue 的重复调用由真实控制器去重。 */
      set(value) { delivery = value.delivery ?? null; lip = value.lip ?? null; runtime.setBehavior(value.behavior ?? 'speaking'); },
      /** 读取官方模型实际参数，不能只检查控制器算出的意图。 */
      read() {
        const parameters = {};
        for (const id of ['ParamEyeSmile','ParamBrowLAngle','ParamBrowRAngle','ParamArmL','ParamMouthOpenY','ParamMouthForm']) {
          const index = runtime.model.getParameterIndex(CubismFramework.getIdManager().getId(id));
          parameters[id] = index >= 0 && index < runtime.model.getParameterCount() ? runtime.model.getParameterValueByIndex(index) : null;
        }
        return { parameters, frames: frames.length, diagnostics: runtime.diagnostics };
      },
      /** 回收场景显式退出 Runtime，避免无界后台动画影响其他项目。 */
      dispose() { runtime.dispose(); },
    };
    document.body.dataset.ready = 'true';
  `;
  const bundle = await build({ stdin: { contents: entry, loader: 'ts', resolveDir: path.join(root, 'web') }, bundle: true, write: false, format: 'esm' });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 960, height: 820 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror',
      /** 保留页面异常，即使图像看似正常也不允许跳过脚本错误。 */
      error => errors.push(error.message));
    page.on('console',
      /** Core 正常日志不算错误，浏览器 error 必须出现在独立证据中。 */
      message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.route('**/*',
      /** 严格拦截所有请求，同源路径只落到显式本地资源目录，测试不会访问互联网。 */
      async route => {
        const url = new URL(route.request().url());
        if (url.origin !== 'http://avatar-verify.invalid') return route.abort();
        if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>小芽表现定向验收</title><style>body{margin:0;background:#f5f7f3;color:#25372a;font:16px system-ui;text-align:center}h1{font-size:22px;margin:38px 0 6px}p{color:#607365;margin:0 0 12px}canvas{width:min(560px,94vw);height:min(560px,94vw);display:block;margin:auto}button{font:inherit;padding:10px 20px;border:1px solid #b9c7b8;border-radius:14px;background:white}</style><h1>小芽 · 表现验收</h1><p>本地合成场景；非真实房间验收</p><canvas></canvas><button onclick="preview.set({behavior:\'idle\'});this.textContent=\'已停止表现\'">停止表现</button><script type="module" src="/verify.js"></script></html>' });
        if (url.pathname === '/verify.js') return route.fulfill({ contentType: 'text/javascript', body: bundle.outputFiles[0].text });
        let base;
        let relative;
        if (url.pathname.startsWith('/avatar/xiaoya/')) {
          base = model; relative = url.pathname.slice('/avatar/xiaoya/'.length);
        } else if (url.pathname.startsWith('/avatar/vendor/')) {
          base = path.join(root, 'web/public/avatar/vendor'); relative = url.pathname.slice('/avatar/vendor/'.length);
        } else return route.abort();
        const file = path.resolve(base, relative);
        if (!file.startsWith(base + path.sep) || !fs.existsSync(file)) return route.fulfill({ status: 404 });
        const contentType = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.json') ? 'application/json' : file.endsWith('.png') ? 'image/png' : 'application/octet-stream';
        return route.fulfill({ body: fs.readFileSync(file), contentType });
      });
    await page.goto('http://avatar-verify.invalid/', { waitUntil: 'networkidle' });
    await page.waitForSelector('body[data-ready="true"]');
    const scenes = [];
    for (const style of ['neutral', 'happy', 'gentle', 'concerned', 'curious']) {
      await page.evaluate(
        /** 参数通过序列化传递，浏览器只调用公开测试场景入口。 */
        style => window.preview.set({ delivery: { id: style, style, gesture: 'none' } }), style);
      await page.waitForTimeout(850);
      await page.screenshot({ path: path.join(output, style + '.png') });
      scenes.push({ style, ...await page.evaluate(
        /** 抓取实际 Core 参数值与非空网格，确认不是一张静态海报。 */
        () => window.preview.read()) });
    }
    await page.evaluate(
      /** 挥手与笑眼同时播放，检查主动作与表情通道互不覆盖。 */
      () => window.preview.set({ delivery: { id: 'wave', style: 'happy', gesture: 'wave' }, lip: { open: 0.45, form: 0.2 } }));
    await page.waitForTimeout(760);
    await page.screenshot({ path: path.join(output, 'happy-wave.png') });
    scenes.push({ style: 'happy-wave', ...await page.evaluate(
      /** 在短动作峰值附近取样，记录真实手臂幅度。 */
      () => window.preview.read()) });
    await page.getByRole('button', { name: '停止表现', exact: true }).click();
    await page.waitForTimeout(750);
    const stopped = await page.evaluate(
      /** 用户停止后必须闭嘴并回正，不能在队列中继续执行旧 cue。 */
      () => window.preview.read());
    if (stopped.parameters.ParamMouthOpenY !== 0 || Math.abs(stopped.parameters.ParamArmL) > 0.01)
      throw new Error('停止表现没有正确清理口型或动作');
    await page.setViewportSize({ width: 390, height: 780 });
    await page.screenshot({ path: path.join(output, 'mobile-stopped.png') });
    const layout = await page.evaluate(
      /** 小屏仅核对本定向场景，不把它当成正式应用整页移动验收。 */
      () => ({ overflow: document.documentElement.scrollWidth > innerWidth, title: document.title, canvas: document.querySelector('canvas').getBoundingClientRect().toJSON() }));
    if (layout.overflow || errors.length) throw new Error(JSON.stringify({ layout, errors }));
    await page.evaluate(
      /** 关闭浏览器前先行释放，用于覆盖显式卸载路径。 */
      () => window.preview.dispose());
    fs.writeFileSync(path.join(output, 'browser-presentation.json'), JSON.stringify({ model, scenes, stopped, layout, errors, privateRoomVerified: false, officialEditorVerificationIncluded: false }, null, 2) + '\n');
    console.log(JSON.stringify({ model, scenes: scenes.map(
      /** 控制终端输出体积，完整几何只保存于本次证据。 */
      scene => ({ style: scene.style, parameters: scene.parameters })), layout, errors }));
  } finally {
    await browser.close();
  }
}

main().catch(
  /** 非零退出码明确区分验收失败与截图文件恰好存在。 */
  error => { console.error(error); process.exitCode = 1; });
