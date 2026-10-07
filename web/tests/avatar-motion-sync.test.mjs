import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { registerHooks } from 'node:module';
import { extname } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// Node 使用原生 TS stripping；官方生成代码沿用无扩展名导入，由测试映射到实际本地文件。
registerHooks({
  /** 仅补 Node 缺少的编译期 alias 与扩展名解析，正式应用仍由 Next.js 使用 tsconfig。 */
  resolve(specifier, context, nextResolve) {
    let url;
    if (specifier.startsWith('@framework/')) {
      url = new URL(
        `../lib/avatar/vendor/cubism/src/${specifier.slice('@framework/'.length)}.js`,
        import.meta.url
      );
    } else if (specifier.startsWith('.') && context.parentURL) {
      const candidate = new URL(specifier, context.parentURL);
      if (!extname(candidate.pathname)) {
        for (const extension of ['.ts', '.js']) {
          const resolved = new URL(candidate.href + extension);
          if (existsSync(fileURLToPath(resolved))) {
            url = resolved;
            break;
          }
        }
      }
    }
    return url ? { url: url.href, shortCircuit: true } : nextResolve(specifier, context);
  },
});

const { MotionSyncAnalyzer, StreamingPcmResampler, createMotionSyncAnalyzer } = await import(
  '../lib/avatar/motion-sync.ts'
);

/** 固定音调用于检查样本相位和窗口时间，不需要访问真实音频或外部模型服务。 */
function tone(length, rate, offset = 0) {
  return Float32Array.from({ length }, (_, index) => {
    // 多个共振分量让官方引擎获得持续的非静音测试输入。
    const time = (offset + index) / rate;
    return (
      Math.sin(time * 2 * Math.PI * 300) * 0.1 +
      Math.sin(time * 2 * Math.PI * 900) * 0.08 +
      Math.sin(time * 2 * Math.PI * 1_800) * 0.06
    );
  });
}

/** 原生端口可控替身检验真实消费边界，保持时序测试独立于供应商的语音算法。 */
function backend(required = 1_440) {
  const observation = { resets: 0, disposed: 0, blocks: [] };
  const engine = {
    sampleRate: 48_000,
    /** 官方引擎可能逐帧改变需求，此端口每次分析前都应被查询。 */
    getRequiredSamples: () => required,
    /** 返回实际消费数量，用于证明两批 PCM 不会丢掉分析余量。 */
    analyze(samples) {
      observation.blocks.push(samples.slice());
      return { consumed: required, open: 0.7, form: -0.4 };
    },
    /** 计数用于区分重连清历史与销毁原生上下文。 */
    reset() {
      observation.resets += 1;
    },
    /** 释放计数用于防止降级加卸载造成双重 free。 */
    dispose() {
      observation.disposed += 1;
    },
  };
  return { engine, observation };
}

/** 每个原生测试独立准备真实 Core，按名称单跑取消场景也不能依赖其他测试留下的全局状态。 */
async function initializeOfficialFramework() {
  const sandbox = vm.createContext({
    console,
    atob,
    performance,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  });
  for (const file of ['live2dcubismcore.min.js', 'live2dcubismmotionsynccore.min.js']) {
    const source = await readFile(
      new URL(`../public/avatar/vendor/${file}`, import.meta.url),
      'utf8'
    );
    try {
      vm.runInContext(source, sandbox, { filename: file });
    } catch (error) {
      throw new Error(`官方 Core 初始化失败：${error.message}`);
    }
  }
  globalThis.Live2DCubismCore = sandbox.Live2DCubismCore;
  globalThis.Live2DCubismMotionSyncCore = sandbox.Live2DCubismMotionSyncCore;
  const { CubismFramework, LogLevel } = await import(
    '../lib/avatar/vendor/cubism/src/live2dcubismframework.js'
  );
  // 官方静态释放入口由 WebGL 适配器注册，测试不创建 GL 上下文。
  await import('../lib/avatar/vendor/cubism/src/rendering/cubismrenderer_webgl.js');
  assert.equal(CubismFramework.startUp({ loggingLevel: LogLevel.LogLevel_Off }), true);
  CubismFramework.initialize();
  return CubismFramework;
}

/** 第一个窗口不足时等待，合并后的分析结果仍对应原始音频中心而非后到达的时间。 */
test('MotionSync 按真实消费数量保留余量及窗口时间戳', () => {
  const { engine, observation } = backend();
  const analyzer = new MotionSyncAnalyzer({ backend: engine });
  assert.deepEqual(analyzer.sample(tone(960, 48_000), 48_000, 1.01), []);
  assert.equal(analyzer.queuedSamples, 960);
  const first = analyzer.sample(tone(960, 48_000, 960), 48_000, 1.03);
  assert.equal(first.length, 1);
  assert.ok(Math.abs(first[0].at - 1.015) < 1e-12);
  assert.equal(first[0].open, 0.7);
  assert.equal(first[0].form, -0.4);
  assert.equal(analyzer.queuedSamples, 480);
  const second = analyzer.sample(tone(960, 48_000, 1_920), 48_000, 1.05);
  assert.ok(Math.abs(second[0].at - 1.045) < 1e-12);
  assert.equal(observation.blocks.length, 2);
  analyzer.dispose();
});

/** 分块插值必须与整段插值相同，长期通话不能每 20 ms 丢失一个小数采样相位。 */
test('44.1 kHz 分块重采样不漂移，结果与整段重采样一致', () => {
  const input = tone(44_100, 44_100);
  const whole = new StreamingPcmResampler().push(input, 44_100);
  const streaming = new StreamingPcmResampler();
  const chunks = [];
  let count = 0;
  for (let offset = 0; offset < input.length; offset += 882) {
    const chunk = streaming.push(input.subarray(offset, offset + 882), 44_100);
    chunks.push(chunk);
    count += chunk.length;
  }
  assert.ok(Math.abs(count - 48_000) <= 1);
  assert.equal(count, whole.length);
  const joined = new Float32Array(count);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.length;
  }
  assert.deepEqual(joined, whole);
  streaming.reset();
  assert.equal(streaming.producedSamples, 0);
});

/** 主线程短暂缺采样后必须清除旧半帧，不能把上一句话当成当前语音的前缀。 */
test('音频间断清除原生历史和 PCM，迟到窗口不能回退时钟', () => {
  const { engine, observation } = backend();
  const analyzer = new MotionSyncAnalyzer({ backend: engine });
  analyzer.sample(tone(480, 48_000), 48_000, 1.005);
  analyzer.sample(tone(480, 48_000), 48_000, 2.005);
  assert.equal(observation.resets, 1);
  assert.equal(analyzer.queuedSamples, 480);
  assert.deepEqual(analyzer.sample(tone(960, 48_000), 48_000, 1.03), []);
  analyzer.reset();
  assert.equal(analyzer.queuedSamples, 0);
  assert.equal(analyzer.mode, 'motion-sync');
  analyzer.dispose();
  analyzer.dispose();
  assert.equal(observation.disposed, 1);
  assert.deepEqual(analyzer.sample(tone(960, 48_000), 48_000, 2.025), []);
});

/** 队列容量以时长限制，供应商错误窗口不能让声音历史无限增长。 */
test('等待引擎窗口时 PCM 有界，非法需求明确降级且只释放一次', () => {
  const { engine, observation } = backend(9_600);
  const modes = [];
  const analyzer = new MotionSyncAnalyzer({
    backend: engine,
    /** 验收必须记录降级原因，不能只收到看似有效的开口参数。 */
    onModeChange: (mode, error) => modes.push({ mode, error }),
  });
  for (let index = 0; index < 100; index += 1) {
    analyzer.sample(tone(960, 48_000), 48_000, 1.01 + index * 0.02);
    assert.ok(analyzer.queuedSamples <= 9_600);
  }
  assert.equal(observation.blocks.length, 10);
  /** 模拟供应商返回不可消费窗口，下一次采样必须给出明确后备状态。 */
  engine.getRequiredSamples = () => 0;
  const frames = analyzer.sample(tone(960, 48_000), 48_000, 3.01);
  assert.equal(frames.length, 1);
  assert.ok(frames[0].open > 0);
  assert.equal(analyzer.mode, 'amplitude');
  assert.equal(modes[0].mode, 'amplitude');
  assert.match(modes[0].error.message, /分析窗口/);
  assert.equal(observation.disposed, 1);
  analyzer.dispose();
  assert.equal(observation.disposed, 1);
});

/** 引擎声音历史可能保留形变，应用静音门限仍须保证真实停顿期间闭嘴。 */
test('静音 PCM 盖过残留嘴型，原生失败后保留音量开合并标记后备', () => {
  const { engine, observation } = backend(480);
  const analyzer = new MotionSyncAnalyzer({ backend: engine });
  const silence = analyzer.sample(new Float32Array(960), 48_000, 1.01);
  assert.ok(silence.every((frame) => frame.open === 0 && frame.form === 0));
  /** 异常是来自供应商的真实失败，不能继续保留上一次原生分析结果。 */
  engine.analyze = () => {
    throw new Error('原生计算失败');
  };
  const speech = analyzer.sample(tone(960, 48_000), 48_000, 1.03);
  assert.equal(analyzer.mode, 'amplitude');
  assert.ok(speech[0].open > 0);
  assert.equal(speech[0].form, 0);
  assert.equal(observation.disposed, 1);
  analyzer.dispose();
});

/** SSR 或脚本加载失败不能阻断语音，但必须准确告诉外层当前只具备音量驱动。 */
test('Core 缺失时创建音量后备并提供失败原因', async () => {
  const modes = [];
  const analyzer = await createMotionSyncAnalyzer(null, new ArrayBuffer(), {
    /** 保存初始化的公开模式和诊断，防止兼容降级被报成成功。 */
    onModeChange: (mode, error) => modes.push({ mode, error }),
  });
  assert.equal(analyzer.mode, 'amplitude');
  assert.match(modes[0].error.message, /尚未加载/);
  analyzer.dispose();
});

/** 预先取消必须先于 Core 可用性校验，缺少脚本也不能把取消错误报成音量后备。 */
test('已取消的 MotionSync 初始化传播原始取消原因，不触发兼容降级', async () => {
  const controller = new AbortController();
  const reason = new DOMException('会话已经结束', 'AbortError');
  let modes = 0;
  controller.abort(reason);
  await assert.rejects(
    createMotionSyncAnalyzer(null, new ArrayBuffer(), {
      signal: controller.signal,
      /** 不允许取消被当成供应商失败，否则旧会话仍可能创建分析器。 */
      onModeChange() {
        modes += 1;
      },
    }),
    (error) => error === reason
  );
  assert.equal(modes, 0);
});

/** 运行真实官方 Core 与 Framework，并用公开分配接口检查多次创建和分析后的释放闭环。 */
test('官方 5-r.2 Core 实际分析成功，重复创建释放不遗留原生分配', async () => {
  const CubismFramework = await initializeOfficialFramework();
  const model = {
    /** 只模拟参数绑定；分析不需要真实模型网格，也不应该写入模型的当前表情。 */
    getParameterCount: () => 2,
    /** 使用官方 ID 管理器，测试真实配置解析的匹配规则。 */
    getParameterId: (index) =>
      CubismFramework.getIdManager().getId(['ParamMouthForm', 'ParamMouthOpenY'][index]),
  };
  const bytes = await readFile(new URL('./fixtures/kei-basic.motionsync3.json', import.meta.url));
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const pointer = globalThis.Live2DCubismMotionSyncCore.ToPointer;
  const malloc = pointer.Malloc;
  const free = pointer.Free;
  const outstanding = new Map();
  /** 统计所有 Framework 持有的原生缓冲，暴露无法被 JS GC 回收的泄漏。 */
  pointer.Malloc = (size) => {
    const address = malloc(size);
    if (address) outstanding.set(address, size);
    return address;
  };
  /** 对称记录公开 free，保留真实引擎内存操作而不是用 Mock 假装释放。 */
  pointer.Free = (address) => {
    outstanding.delete(address);
    return free(address);
  };
  try {
    let count = 0;
    let maximumOpen = 0;
    let maximumForm = 0;
    const first = await createMotionSyncAnalyzer(model, buffer);
    const second = await createMotionSyncAnalyzer(model, buffer);
    assert.equal(first.mode, 'motion-sync');
    assert.equal(second.mode, 'motion-sync');
    first.dispose();
    assert.ok(outstanding.size > 0);
    assert.ok(second.sample(tone(960, 48_000), 48_000, 1.01).length > 0);
    assert.equal(second.mode, 'motion-sync');
    second.dispose();
    assert.equal(outstanding.size, 0);
    for (let session = 0; session < 20; session += 1) {
      const failures = [];
      const analyzer = await createMotionSyncAnalyzer(model, buffer, {
        /** 任意官方初始化或分析失败都让此验收失败，不能借音量后备通过。 */
        onModeChange: (_, error) => failures.push(error),
      });
      assert.equal(analyzer.mode, 'motion-sync', failures[0]?.stack);
      for (let index = 0; index < 20; index += 1) {
        const frames = analyzer.sample(tone(960, 48_000, index * 960), 48_000, 1.01 + index * 0.02);
        count += frames.length;
        for (const frame of frames) {
          maximumOpen = Math.max(maximumOpen, frame.open);
          maximumForm = Math.max(maximumForm, Math.abs(frame.form));
          assert.ok(Number.isFinite(frame.open) && Number.isFinite(frame.form));
          assert.ok(frame.open >= 0 && frame.open <= 1);
          assert.ok(frame.form >= -1 && frame.form <= 1);
          assert.ok(frame.at >= 1 && frame.at <= 1.4);
        }
      }
      assert.equal(analyzer.mode, 'motion-sync', failures[0]?.stack);
      analyzer.reset();
      analyzer.dispose();
      analyzer.dispose();
      assert.equal(outstanding.size, 0, `会话 ${session + 1} 遗留原生指针`);
    }
    assert.ok(count > 0);
    assert.ok(maximumOpen > 0.02, '真实原生结果应包含可见开口，不能只输出零值');
    assert.ok(maximumForm > 0.02, '真实原生结果应包含嘴型形变，不能只有音量开合');
  } finally {
    pointer.Malloc = malloc;
    pointer.Free = free;
    CubismFramework.dispose();
  }
});

/** 真正跨越动态导入的异步边界取消，模拟舞台已销毁模型，防止只检查入口而漏掉迟到导入。 */
test('官方模块异步导入期间取消不会读取已销毁模型或创建音量后备', async () => {
  const CubismFramework = await initializeOfficialFramework();
  const bytes = await readFile(new URL('./fixtures/kei-basic.motionsync3.json', import.meta.url));
  const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  const controller = new AbortController();
  const reason = new DOMException('舞台在导入完成前卸载', 'AbortError');
  let disposed = false;
  let disposedModelReads = 0;
  let modes = 0;
  const model = {
    /** 销毁后的访问计数比仅检查返回值更严格，能直接复现旧工厂迟到访问原生模型的竞态。 */
    getParameterCount() {
      if (disposed) {
        disposedModelReads += 1;
        throw new Error('禁止读取已销毁模型');
      }
      return 2;
    },
    /** 使用真实官方 ID 管理器，使未取消路径可以继续解析有效模型参数。 */
    getParameterId: (index) =>
      CubismFramework.getIdManager().getId(['ParamMouthForm', 'ParamMouthOpenY'][index]),
  };
  try {
    const pending = createMotionSyncAnalyzer(model, buffer, {
      signal: controller.signal,
      /** 模块导入取消需要直接 reject，不应以 amplitude 状态通知调用者继续使用旧模型。 */
      onModeChange() {
        modes += 1;
      },
    });
    assert.equal(disposedModelReads, 0);
    assert.equal(modes, 0);
    disposed = true;
    controller.abort(reason);
    await assert.rejects(pending, (error) => error === reason);
    assert.equal(disposedModelReads, 0);
    assert.equal(modes, 0);
  } finally {
    CubismFramework.dispose();
  }
});
