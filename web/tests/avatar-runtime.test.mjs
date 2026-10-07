import { build } from 'esbuild';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// SDK 替身只模拟所有权边界，测试执行实际运行时的加载、事件和销毁代码。
const sdkFixture = `
const observation = globalThis.observation;
export const LogLevel = { LogLevel_Error: 0 };
export const CubismFramework = {
  /** 测试只关注加载后的回收，不需要真实原生 Core。 */
  startUp() { return true; },
  /** 无原生分配时保持初始化可调用，所有权由 renderer 和 moc 分别记录。 */
  initialize() {},
  /** 返回稳定 ID 以便实际 draw 路径完成参数更新。 */
  getIdManager() {
    return {
      /** ID 原样返回让真实运行时完成参数查找，不引入与回收测试无关的 SDK 缓存。 */
      getId: (name) => name,
    };
  },
};
export class CubismMatrix44 {
  /** 此测试不验证投影，保留绘制调用而省去矩阵算法。 */
  scale() {}
}
export class CubismMoc {
  /** 保留实例供失败后核对模型和 moc 的释放，避免只检查 Promise 拒绝。 */
  static create() {
    const moc = new CubismMoc();
    observation.mocs.push(moc);
    return moc;
  }
  releases = 0;
  deletedModels = 0;
  /** 稳定最小模型使异常来自 SDK 初始化，而不是输入文件缺失。 */
  createModel() {
    return {
      /** 唯一且合法的绘制层级让加载越过模型校验，失败只能来自本测试注入的 SDK 步骤。 */
      getDrawableRenderOrders: () => [0],
      /** 不持有可写的真实参数，生命周期测试不会意外模拟或依赖网格变形。 */
      getParameterIndex: () => -1,
      /** 固定高度避免投影分母为零，确保首帧验证聚焦资源所有权。 */
      getCanvasHeight: () => 2,
      /** 与高度一致的尺寸排除模型比例差异，真实视觉表现留给浏览器验收。 */
      getCanvasWidth: () => 2,
      /** 参数更新不再分配资源，防止替身掩盖宿主在失败和卸载时的释放责任。 */
      update() {},
    };
  }
  /** 模型删除与 moc 本体释放是两个必要操作，必须分别验证。 */
  deleteModel() { this.deletedModels += 1; }
  /** 计数可发现取消与重复卸载导致的重复原生释放。 */
  release() { this.releases += 1; }
}
export class CubismRenderer_WebGL {
  releases = 0;
  /** 构造尚未持有 GL 资源，所有可失败步骤通过控制点注入。 */
  constructor() { observation.renderers.push(this); }
  /** 模拟 clipping manager 初始化中途失败，真实运行时仍须回收 renderer。 */
  initialize() {
    if (observation.failure === 'initialize') throw new Error('模型渲染器初始化失败');
  }
  /** 模拟驱动或扩展查询失败，防止尚未返回的构造函数遗留 GL 引用。 */
  startUp() {
    if (observation.failure === 'startUp') throw new Error('WebGL 启动失败');
  }
  /** 参数设置没有额外资源，测试保留实际宿主调用顺序。 */
  setIsPremultipliedAlpha() {}
  /** 核对统一释放入口，而不是依赖垃圾回收推测 GPU 已释放。 */
  release() { this.releases += 1; }
  /** 不模拟绘制算法，仅提供完成首帧所需的 SDK 协议。 */
  setMvpMatrix() {}
  /** 渲染目标本身由宿主 GL 管理，SDK 替身不需要额外状态。 */
  setRenderState() {}
  /** 首帧计数证明事件测试在成功加载之后开始。 */
  drawModel() { observation.draws += 1; }
}
export const CubismShaderManager_WebGL = {
  /** 共享 manager 只能在最后一个舞台退出后释放，失败重试也受此边界约束。 */
  deleteInstance() { observation.shaderReleases += 1; },
};
export const CubismPhysics = {};
/** 不加载外部脚本，网络和官方引擎兼容性已有独立验收。 */
export async function loadSdkScript() {}
`;

const compiled = await build({
  entryPoints: [fileURLToPath(new URL('../lib/avatar/live2d-runtime.ts', import.meta.url))],
  bundle: true,
  write: false,
  format: 'iife',
  globalName: 'AvatarRuntimeModule',
  plugins: [
    {
      name: 'runtime-resource-fixture',
      /** 只替换 SDK 和脚本入口，加载与资源生命周期始终来自正式源码。 */
      setup(builder) {
        /** SDK 适配边界之外的行为和生命周期模块保持正式实现。 */
        builder.onResolve({ filter: /vendor\/cubism\/|sdk-loader/ }, (arguments_) => ({
          path: arguments_.path,
          namespace: 'runtime-resource-fixture',
        }));
        /** 每个 SDK 路径保留具名导出，由 bundler 选择实际使用的协议。 */
        builder.onLoad({ filter: /.*/, namespace: 'runtime-resource-fixture' }, () => ({
          contents: sdkFixture,
          loader: 'js',
        }));
      },
    },
  ],
});

/** 资源回收测试保持闭嘴，不引入分析器或其他资源所有者。 */
function readClosedLip() {
  return null;
}

/** 资源响应与 SDK 故障分别可注入，保证表情降级测试也执行真实加载和回收路径。 */
function fixture({ expressions = [], expressionFiles = {}, beforeExpression } = {}) {
  const observation = {
    mocs: [],
    renderers: [],
    shaderReleases: 0,
    draws: 0,
    observers: [],
    requested: new Set(),
    failure: '',
    expressionRequests: [],
  };
  let frameId = 0;
  const gl = {
    NO_ERROR: 0,
    /** 上下文丢失状态由事件测试显式驱动，不访问真实 GPU。 */
    isContextLost: () => false,
    /** 成功路径通过首帧检查，初始化失败由 SDK 控制点独立注入。 */
    getError: () => 0,
    /** 只记录生命周期；投影和纹理表现由真实浏览器验收负责。 */
    viewport() {},
    /** 透明底板是渲染契约，测试无真实帧缓冲时允许调用。 */
    clearColor() {},
    /** 保留首帧流程以验证 resize 与观察器在成功后才注册。 */
    clear() {},
    /** 返回默认帧缓冲，避免从无关 GL 状态构造失败。 */
    getParameter: () => null,
  };
  const sandbox = vm.createContext({
    observation,
    URL,
    TextDecoder,
    console,
    location: { origin: 'http://localhost' },
    devicePixelRatio: 1,
    /** 表情可独立缺失、损坏或取消，其余模型资源保持成功以隔离降级边界。 */
    async fetch(url) {
      let resource;
      if (url.pathname.includes('/expressions/')) {
        const name = url.pathname.split('/').at(-1);
        observation.expressionRequests.push(name);
        beforeExpression?.(name);
        resource = expressionFiles[name];
      }
      const data = new TextEncoder().encode(
        resource !== undefined
          ? typeof resource === 'string'
            ? resource
            : JSON.stringify(resource)
          : url.pathname.endsWith('.json')
            ? JSON.stringify({
                FileReferences: { Moc: 'xiaoya.moc3', Textures: [], Expressions: expressions },
              })
            : 'model'
      );
      return {
        ok: resource !== null,
        /** 保留异步响应协议并复用请求持有的缓冲，测试无网络或额外下载资源。 */
        arrayBuffer: async () => data.buffer,
      };
    },
    performance: {
      /** 固定首帧时刻让请求动画与取消断言不受宿主机器速度影响。 */
      now: () => 1_000,
    },
    /** 保存待执行动画数，失败、丢失和卸载必须删除先前安排的帧。 */
    requestAnimationFrame() {
      const id = ++frameId;
      observation.requested.add(id);
      return id;
    },
    /** 可观察的帧取消可发现只隐藏 canvas 却仍在后台运行的情况。 */
    cancelAnimationFrame(id) {
      observation.requested.delete(id);
    },
    ResizeObserver: class {
      disconnected = false;
      /** 每个观察器具有独立生命周期，重试不能遗留旧 canvas 引用。 */
      constructor() {
        observation.observers.push(this);
      }
      /** DOM 尺寸变化在本测试中保持固定。 */
      observe() {}
      /** 卸载观察器是释放 canvas 的必要条件。 */
      disconnect() {
        this.disconnected = true;
      }
    },
  });
  vm.runInContext(compiled.outputFiles[0].text, sandbox);
  const listeners = new Map();
  const canvas = {
    /** 复用可控 GL 对象，测试失败与后续重试使用同一驱动协议。 */
    getContext: () => gl,
    /** 固定大小让首帧完成，排除响应式布局对资源测试的干扰。 */
    getBoundingClientRect: () => ({ width: 400, height: 400 }),
    /** 保存真实运行时注册的引用，事件可在释放前后逐个核对。 */
    addEventListener: (name, callback) => listeners.set(name, callback),
    /** 只删除对应引用，模拟旧舞台不得误删后来重试的新监听。 */
    removeEventListener(name, callback) {
      if (listeners.get(name) === callback) listeners.delete(name);
    },
  };
  return { Runtime: sandbox.AvatarRuntimeModule.Live2DRuntime, canvas, observation, listeners };
}

/** 初始化失败必须回收半初始化 renderer、模型和共享 GL 缓存，明确重试后仍能成功。 */
test('SDK 初始化和 GL 启动中途失败都完整回收，再次加载不泄漏', async () => {
  for (const failure of ['initialize', 'startUp']) {
    const { Runtime, canvas, observation, listeners } = fixture();
    observation.failure = failure;
    await assert.rejects(Runtime.load(canvas, {}, new AbortController().signal), /失败/);
    assert.equal(observation.renderers[0].releases, 1);
    assert.equal(observation.mocs[0].deletedModels, 1);
    assert.equal(observation.mocs[0].releases, 1);
    assert.equal(observation.shaderReleases, 1);
    assert.equal(observation.requested.size, 0);
    assert.equal(listeners.size, 0);
    observation.failure = '';
    const runtime = await Runtime.load(
      canvas,
      { readLip: readClosedLip },
      new AbortController().signal
    );
    runtime.dispose();
    runtime.dispose();
    assert.equal(observation.renderers[1].releases, 1);
    assert.equal(observation.mocs[1].deletedModels, 1);
    assert.equal(observation.mocs[1].releases, 1);
    assert.equal(observation.shaderReleases, 2);
    assert.equal(observation.requested.size, 0);
    assert.ok(
      observation.observers.every(
        /** 每个观察器都应解绑；仅检查最后一个会漏掉初始化重试留下的旧 canvas 引用。 */
        (observer) => observer.disconnected
      )
    );
  }
});

/** 局部失败不能清掉仍在显示的舞台所用共享缓存，最后一个消费者离开才释放一次。 */
test('另一舞台初始化失败不会释放仍被正常舞台使用的 shader manager', async () => {
  const { Runtime, canvas, observation } = fixture();
  const current = await Runtime.load(
    canvas,
    { readLip: readClosedLip },
    new AbortController().signal
  );
  observation.failure = 'startUp';
  await assert.rejects(Runtime.load(canvas, {}, new AbortController().signal), /失败/);
  assert.equal(observation.shaderReleases, 0);
  current.dispose();
  assert.equal(observation.shaderReleases, 1);
  assert.ok(
    observation.renderers.every(
      /** 成功与失败的消费者分别且仅释放一次，避免共享 manager 的计数掩盖局部泄漏。 */
      (renderer) => renderer.releases === 1
    )
  );
  assert.ok(
    observation.mocs.every(
      /** 模型实例和 moc 本体各有释放责任，不能只释放其中一个就判定原生资源已回收。 */
      (moc) => moc.releases === 1 && moc.deletedModels === 1
    )
  );
});

/** GL 丢失停止绘制但保留恢复监听，舞台退出后迟到恢复不得再启动新加载。 */
test('WebGL 丢失进入后备并允许恢复，销毁后移除恢复监听', async () => {
  const { Runtime, canvas, observation, listeners } = fixture();
  let faults = 0;
  let restores = 0;
  const runtime = await Runtime.load(
    canvas,
    {
      /** 实际口型不参与 GL 事件测试，始终保持闭嘴。 */
      readLip: readClosedLip,
      /** 故障通知代表用户界面切换静态后备。 */
      onFault: () => (faults += 1),
      /** 恢复通知代表组件启动一次新的资源加载。 */
      onRestore: () => (restores += 1),
    },
    new AbortController().signal
  );
  const lost = new Event('webglcontextlost', { cancelable: true });
  listeners.get('webglcontextlost')(lost);
  assert.equal(lost.defaultPrevented, true);
  assert.equal(faults, 1);
  assert.equal(observation.requested.size, 0);
  const restored = listeners.get('webglcontextrestored');
  restored();
  assert.equal(restores, 1);
  runtime.dispose();
  restored();
  assert.equal(restores, 1);
  assert.equal(listeners.size, 0);
  assert.equal(observation.shaderReleases, 1);
});

/** 缺失与损坏的可选表情不能废弃已加载人物，且不得阻止后续其他表情继续加载。 */
test('单份表情 404、非法 JSON 或非法参数只降级该表情', async () => {
  for (const invalid of [
    null,
    '{broken-json',
    {
      Type: 'Live2D Expression',
      Parameters: [{ Id: 'ParamMouthOpenY', Value: 1, Blend: 'Overwrite' }],
    },
  ]) {
    const { Runtime, canvas, observation } = fixture({
      expressions: [
        { Name: 'happy', File: 'expressions/happy.exp3.json' },
        { Name: 'gentle', File: 'expressions/gentle.exp3.json' },
      ],
      expressionFiles: {
        'happy.exp3.json': invalid,
        'gentle.exp3.json': { Type: 'Live2D Expression', Parameters: [] },
      },
    });
    const runtime = await Runtime.load(
      canvas,
      { readLip: readClosedLip },
      new AbortController().signal
    );
    assert.deepEqual(observation.expressionRequests, ['happy.exp3.json', 'gentle.exp3.json']);
    assert.equal(observation.draws, 1);
    assert.equal(observation.requested.size, 1);
    assert.equal(observation.mocs[0].releases, 0);
    runtime.dispose();
    assert.equal(observation.mocs[0].releases, 1);
    assert.equal(observation.renderers[0].releases, 1);
  }
});

/** 用户退出不是资源缺失，表情下载中取消必须拒绝整个加载并完整释放半初始化对象。 */
test('表情下载中取消不被可选资源降级吞掉', async () => {
  const cancellation = new AbortController();
  const { Runtime, canvas, observation, listeners } = fixture({
    expressions: [
      { Name: 'happy', File: 'expressions/happy.exp3.json' },
      { Name: 'gentle', File: 'expressions/gentle.exp3.json' },
    ],
    expressionFiles: { 'happy.exp3.json': null },
    /** 在读取第一份可选资源时取消，使 catch 必须识别生命周期而非吞掉中止。 */
    beforeExpression: () => cancellation.abort(),
  });
  await assert.rejects(Runtime.load(canvas, { readLip: readClosedLip }, cancellation.signal), {
    name: 'AbortError',
  });
  assert.deepEqual(observation.expressionRequests, ['happy.exp3.json']);
  assert.equal(observation.draws, 0);
  assert.equal(observation.mocs[0].releases, 1);
  assert.equal(observation.mocs[0].deletedModels, 1);
  assert.equal(observation.renderers[0].releases, 1);
  assert.equal(observation.shaderReleases, 1);
  assert.equal(observation.requested.size, 0);
  assert.equal(listeners.size, 0);
});
