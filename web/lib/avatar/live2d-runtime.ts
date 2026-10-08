import { type AvatarBehavior, advanceFrameDeadline } from './behavior';
import type { LipSyncFrame } from './lip-sync';
import {
  type AvatarDelivery,
  type AvatarExpressions,
  AvatarPresentation,
  isAvatarExpressionName,
  parseAvatarExpression,
} from './presentation';
import { loadSdkScript } from './sdk-loader';
import { CubismFramework, LogLevel } from './vendor/cubism/src/live2dcubismframework';
import { CubismMatrix44 } from './vendor/cubism/src/math/cubismmatrix44';
import { CubismMoc } from './vendor/cubism/src/model/cubismmoc';
import type { CubismModel } from './vendor/cubism/src/model/cubismmodel';
import { CubismPhysics } from './vendor/cubism/src/physics/cubismphysics';
import { CubismRenderer_WebGL } from './vendor/cubism/src/rendering/cubismrenderer_webgl';
import { CubismShaderManager_WebGL } from './vendor/cubism/src/rendering/cubismshader_webgl';

type ModelManifest = {
  FileReferences: {
    Moc: string;
    Textures: string[];
    Physics?: string;
    MotionSync?: string;
    Expressions?: { Name: string; File: string }[];
  };
};
export type AvatarFrame = { time: number; open: number; form: number; behavior: AvatarBehavior };
type RuntimeOptions = {
  readLip: () => LipSyncFrame | null;
  readDelivery?: () => AvatarDelivery | null;
  onFault: () => void;
  onRestore: () => void;
  onFrame?: (frame: AvatarFrame) => void;
};
let frameworkReady = false;
let activeRuntimes = 0;

/** 单个页面共享官方 Framework 的 ID 管理器；模型、纹理和 GL 对象始终按舞台独立回收。 */
async function initializeFramework() {
  await loadSdkScript('/avatar/vendor/live2dcubismcore.min.js');
  if (frameworkReady) return;
  if (
    !CubismFramework.startUp({ loggingLevel: LogLevel.LogLevel_Error, logFunction: console.warn })
  )
    throw new Error('人物引擎初始化失败');
  CubismFramework.initialize();
  frameworkReady = true;
}

/** 资源必须同源且在模型目录下，损坏的配置不能把私有应用带到外部请求。 */
function modelResource(base: URL, name: string): URL {
  const url = new URL(name, base);
  if (url.origin !== base.origin || !url.pathname.startsWith(base.pathname))
    throw new Error('模型资源路径无效');
  return url;
}

/** 下载重新核对 ETag，原画更新后不能继续用旧模型；失败和取消仍传播到可重试的静态后备。 */
async function readResource(url: URL, signal: AbortSignal): Promise<ArrayBuffer> {
  signal.throwIfAborted();
  const response = await fetch(url, { signal, cache: 'no-cache' });
  if (!response.ok) throw new Error('人物资源加载失败');
  return response.arrayBuffer();
}

export class Live2DRuntime {
  readonly model: CubismModel;
  readonly motionSyncBuffer?: ArrayBuffer;
  private readonly gl: WebGLRenderingContext;
  private readonly moc: CubismMoc;
  private readonly renderer: CubismRenderer_WebGL;
  private readonly textures: WebGLTexture[] = [];
  private physics?: CubismPhysics;
  private behavior: AvatarBehavior = 'idle';
  private frame = 0;
  private previous = 0;
  private nextBlink = 1.8 + Math.random() * 2;
  private blinkAt = -10;
  private nextLook = 0;
  private gaze = { x: 0, y: 0 };
  private presentation = new AvatarPresentation();
  private disposed = false;
  private observer?: ResizeObserver;
  private frameInterval = 1000 / 60;
  private nextFrameAt = 0;
  private slowFrames = 0;

  /** 首帧前完成全部必要资源和一致性校验；局部失败也沿统一销毁路径释放 native 模型。 */
  static async load(canvas: HTMLCanvasElement, options: RuntimeOptions, signal: AbortSignal) {
    await initializeFramework();
    signal.throwIfAborted();
    const base = new URL('/avatar/xiaoya/', location.origin);
    const manifest = JSON.parse(
      new TextDecoder().decode(await readResource(new URL('xiaoya.model3.json', base), signal))
    ) as ModelManifest;
    // 下载相互独立的资源时不占用 native/GPU 所有权，取消或网络失败无需回收半初始化模型。
    const [mocBuffer, textureBuffers, physicsBuffer, sync, expressionBuffers] = await Promise.all([
      readResource(modelResource(base, manifest.FileReferences.Moc), signal),
      Promise.all(
        manifest.FileReferences.Textures.map((name) =>
          readResource(modelResource(base, name), signal)
        )
      ),
      manifest.FileReferences.Physics
        ? readResource(modelResource(base, manifest.FileReferences.Physics), signal)
        : undefined,
      manifest.FileReferences.MotionSync
        ? readResource(modelResource(base, manifest.FileReferences.MotionSync), signal)
        : undefined,
      Promise.all(
        (manifest.FileReferences.Expressions ?? []).map(async (expression) => {
          try {
            return {
              name: expression.Name,
              buffer: await readResource(modelResource(base, expression.File), signal),
            };
          } catch {
            signal.throwIfAborted();
            return null;
          }
        })
      ),
    ]);
    signal.throwIfAborted();
    const moc = CubismMoc.create(mocBuffer, true);
    if (!moc) throw new Error('人物模型校验失败');
    const model = moc.createModel();
    if (!model) {
      moc.release();
      throw new Error('人物模型初始化失败');
    }
    const orders = Array.from(model.getDrawableRenderOrders());
    if (
      orders.length === 0 ||
      new Set(orders).size !== orders.length ||
      orders.some((order) => order < 0 || order >= orders.length)
    ) {
      moc.deleteModel(model);
      moc.release();
      throw new Error('人物渲染层级无效');
    }
    let runtime: Live2DRuntime | undefined;
    try {
      const gl = canvas.getContext('webgl', {
        alpha: true,
        premultipliedAlpha: true,
        antialias: true,
      });
      if (!gl || gl.isContextLost()) throw new Error('当前设备无法显示动画');
      runtime = new Live2DRuntime(canvas, gl, moc, model, sync, options);
      // 先建立资源所有者再调用可能抛错的 SDK，半初始化的 renderer 也进入统一释放路径。
      runtime.renderer.initialize(model);
      runtime.renderer.startUp(gl);
      runtime.renderer.setIsPremultipliedAlpha(true);
      for (const [index, bytes] of textureBuffers.entries()) {
        const bitmap = await createImageBitmap(new Blob([bytes]), {
          premultiplyAlpha: 'premultiply',
        });
        try {
          signal.throwIfAborted();
          runtime.bindTexture(index, bitmap);
        } finally {
          bitmap.close();
        }
      }
      if (physicsBuffer) {
        runtime.physics = CubismPhysics.create(physicsBuffer, physicsBuffer.byteLength);
      }
      const expressions: AvatarExpressions = {};
      for (const expression of expressionBuffers) {
        if (!expression || !isAvatarExpressionName(expression.name)) continue;
        try {
          if (expressions[expression.name]) throw new Error('人物表情名称重复');
          expressions[expression.name] = parseAvatarExpression(
            JSON.parse(new TextDecoder().decode(expression.buffer))
          );
        } catch {
          // 表情是可选装饰：单份缺失或损坏只保留该风格的基线，不废弃可用模型。
          // 取消属于舞台生命周期而非资源降级，必须传播以释放已分配的 GPU/native 资源。
          signal.throwIfAborted();
        }
      }
      runtime.presentation = new AvatarPresentation(expressions);
      signal.throwIfAborted();
      runtime.resize();
      runtime.draw(performance.now());
      runtime.nextFrameAt = runtime.previous + runtime.frameInterval;
      if (gl.getError() !== gl.NO_ERROR) throw new Error('人物首帧渲染失败');
      runtime.observer = new ResizeObserver(() => runtime?.resize());
      runtime.observer.observe(canvas);
      runtime.frame = requestAnimationFrame(runtime.tick);
      return runtime;
    } catch (error) {
      if (runtime) runtime.dispose();
      else {
        moc.deleteModel(model);
        moc.release();
      }
      throw error;
    }
  }

  /** 构造只登记所有权；可失败的 SDK 初始化留在 load 内，使局部资源也能被完整回收。 */
  private constructor(
    private readonly canvas: HTMLCanvasElement,
    gl: WebGLRenderingContext,
    moc: CubismMoc,
    model: CubismModel,
    sync: ArrayBuffer | undefined,
    private readonly options: RuntimeOptions
  ) {
    this.gl = gl;
    this.moc = moc;
    this.model = model;
    this.motionSyncBuffer = sync;
    this.renderer = new CubismRenderer_WebGL();
    activeRuntimes++;
    canvas.addEventListener('webglcontextlost', this.contextLost);
    canvas.addEventListener('webglcontextrestored', this.contextRestored);
  }

  /** 上传后释放解码图像，纹理归该 GL 舞台所有，避免重试累计 GPU 内存。 */
  private bindTexture(index: number, image: ImageBitmap) {
    const gl = this.gl;
    const texture = gl.createTexture();
    if (!texture) throw new Error('人物纹理初始化失败');
    this.textures.push(texture);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.renderer.bindTexture(index, texture);
  }

  /** 设备像素比限制为 2，固定舞台比例让高 DPI 屏幕清晰而不无谓增加移动端负担。 */
  resize() {
    const bounds = this.canvas.getBoundingClientRect();
    const ratio = Math.min(devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(bounds.width * ratio));
    this.canvas.height = Math.max(1, Math.round(bounds.height * ratio));
  }

  /** 状态更新不重启动画时钟，也不涉及嘴部，避免表情覆盖真实音频口型。 */
  setBehavior(behavior: AvatarBehavior) {
    this.behavior = behavior;
  }

  /** 仅返回渲染数值，供本地验收定位透明或越界网格；不暴露音频、会话和框架对象。 */
  get diagnostics() {
    return Array.from({ length: this.model.getDrawableCount() }, (_, index) => {
      const vertices = this.model.getDrawableVertices(index);
      const x = Array.from(vertices).filter((_, position) => position % 2 === 0);
      const y = Array.from(vertices).filter((_, position) => position % 2 === 1);
      return {
        index,
        texture: this.model.getDrawableTextureIndex(index),
        order: this.model.getDrawableRenderOrders()[index],
        uv: Array.from(this.model.getDrawableVertexUvs(index)).slice(0, 8),
        opacity: this.model.getDrawableOpacity(index),
        visible: this.model.getDrawableDynamicFlagIsVisible(index),
        bounds: [Math.min(...x), Math.max(...x), Math.min(...y), Math.max(...y)],
      };
    });
  }

  /** 连续慢帧自动降至 30 fps；高频参数直接写模型，不触发 React 重渲染。 */
  private tick = (time: number) => {
    if (this.disposed || this.gl.isContextLost()) return;
    try {
      if (time + 0.5 >= this.nextFrameAt) {
        if (this.previous && time - this.previous > 25) this.slowFrames++;
        else this.slowFrames = Math.max(0, this.slowFrames - 1);
        if (this.slowFrames > 120) this.frameInterval = 1000 / 30;
        this.draw(time);
        this.nextFrameAt = advanceFrameDeadline(this.nextFrameAt, time, this.frameInterval);
      }
      this.frame = requestAnimationFrame(this.tick);
    } catch {
      this.options.onFault();
    }
  };

  /** 合成器统一动作、音频独占嘴部；沿原画留白投影，避免动画与同款后备图切换时跳动。 */
  private draw(time: number) {
    const seconds = time / 1000;
    const dt = this.previous ? (time - this.previous) / 1000 : 1 / 60;
    this.previous = time;
    if (seconds > this.nextLook) {
      this.gaze = { x: (Math.random() - 0.5) * 0.45, y: (Math.random() - 0.5) * 0.2 };
      this.nextLook = seconds + 2.2 + Math.random() * 4;
    }
    if (seconds > this.nextBlink) {
      this.blinkAt = seconds;
      this.nextBlink = seconds + 2.7 + Math.random() * 4.4;
    }
    const blinkAge = seconds - this.blinkAt;
    const blink = blinkAge < 0.16 ? 1 - Math.sin((blinkAge / 0.16) * Math.PI) : 1;
    const lip = this.options.readLip();
    const parameters = this.presentation.advance({
      time: seconds,
      delta: dt,
      behavior: this.behavior,
      delivery: this.options.readDelivery?.() ?? null,
      gaze: this.gaze,
      blink,
      lip,
    });
    for (const [name, value] of Object.entries(parameters)) {
      if (name !== 'ParamMouthOpenY' && name !== 'ParamMouthForm') this.setParameter(name, value);
    }
    this.physics?.evaluate(this.model, Math.max(0, Math.min(dt, 0.05)));
    this.setParameter('ParamMouthOpenY', parameters.ParamMouthOpenY);
    this.setParameter('ParamMouthForm', parameters.ParamMouthForm);
    this.model.update();
    const gl = this.gl;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const matrix = new CubismMatrix44();
    const aspect = this.canvas.width / this.canvas.height;
    const scale = 2 / Math.max(this.model.getCanvasHeight(), this.model.getCanvasWidth() / aspect);
    matrix.scale(scale / aspect, scale);
    this.renderer.setMvpMatrix(matrix);
    this.renderer.setRenderState(gl.getParameter(gl.FRAMEBUFFER_BINDING), [
      0,
      0,
      this.canvas.width,
      this.canvas.height,
    ]);
    this.renderer.drawModel();
    this.options.onFrame?.({
      time: seconds,
      open: parameters.ParamMouthOpenY,
      form: parameters.ParamMouthForm,
      behavior: this.behavior,
    });
  }

  /** 模型参数必须真实存在，SDK 的虚拟未知参数不能掩盖缺失绑定。 */
  private setParameter(name: string, value: number) {
    const id = CubismFramework.getIdManager().getId(name);
    const index = this.model.getParameterIndex(id);
    if (index >= 0 && index < this.model.getParameterCount())
      this.model.setParameterValueByIndex(index, value);
  }

  /** 丢失 GL 时立即退到静态，允许浏览器恢复；语音资源不属于舞台，因此继续播放。 */
  private contextLost = (event: Event) => {
    event.preventDefault();
    cancelAnimationFrame(this.frame);
    this.options.onFault();
  };
  /** 恢复后重新装载纹理和 renderer，避免沿用失效的 GPU 句柄。 */
  private contextRestored = () => {
    if (!this.disposed) this.options.onRestore();
  };

  /** 幂等销毁覆盖加载失败、减少动态、重试和卸载，且从不触碰 SDK 音轨。 */
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.observer?.disconnect();
    this.canvas.removeEventListener('webglcontextlost', this.contextLost);
    this.canvas.removeEventListener('webglcontextrestored', this.contextRestored);
    this.physics?.release();
    this.renderer.release();
    // 官方shader manager按GL强引用缓存，最后一个舞台释放时必须同时删除程序和context引用。
    if (--activeRuntimes === 0) CubismShaderManager_WebGL.deleteInstance();
    for (const texture of this.textures) this.gl.deleteTexture(texture);
    this.moc.deleteModel(this.model);
    this.moc.release();
  }
}
