import React from 'react';
import { build } from 'esbuild';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);

/** 打包真实模块但只替换浏览器、SDK 与模型端口，React 和生产 hook 都照常运行。 */
export async function production(path, ports = {}) {
  const compiled = await build({
    entryPoints: [fileURLToPath(new URL('../../' + path, import.meta.url))],
    bundle: true,
    write: false,
    format: 'cjs',
    platform: 'node',
    jsx: 'automatic',
    external: [
      'react',
      'react/jsx-runtime',
      'livekit-client',
      '@livekit/components-react',
      'next/image',
      'next/link',
      'next-themes',
      'lucide-react',
      'radix-ui',
      '*.png',
      ...Object.keys(ports),
    ],
    define: { 'process.env.NODE_ENV': '"test"' },
    plugins: [
      {
        name: 'external-ports',
        /** 动态模型模块仍经过真实 Promise 边界，但不能调用 native import 绕过声明的离线端口。 */
        setup(builder) {
          builder.onResolve({ filter: /.*/ }, (args) => {
            if (!Object.hasOwn(ports, args.path)) return;
            return args.kind === 'dynamic-import'
              ? { path: args.path, namespace: 'external-port' }
              : { path: args.path, external: true };
          });
          builder.onLoad({ filter: /.*/, namespace: 'external-port' }, (args) => ({
            contents: 'module.exports = require(' + JSON.stringify(args.path) + ');',
            loader: 'js',
          }));
        },
      },
    ],
  });
  const compiledModule = { exports: {} };
  /** 自制 JSX 树不能检验真实提交；模块加载器仅接入可控外部端口。 */
  function dependency(name) {
    if (Object.hasOwn(ports, name)) return ports[name];
    if (name.endsWith('.png')) return { src: '/avatar/test-poster.png', width: 1280, height: 1280 };
    if (name === 'next/image') return Image;
    if (name === 'next/link') return Link;
    if (name === 'next-themes')
      return {
        useTheme: () => ({
          theme: 'light',
          /** 主题持久化是外部端口，DOM 用例保持固定主题而不模拟全局存储。 */
          setTheme() {},
        }),
      };
    return require(name);
  }
  new Function('require', 'module', 'exports', compiled.outputFiles[0].text)(
    dependency,
    compiledModule,
    compiledModule.exports
  );
  return compiledModule.exports;
}

/** Next 图像优化属于外部端口；真实 DOM 仍保留可访问标签和静态切换样式。 */
function Image({ src, priority, ...props }) {
  void priority;
  return React.createElement('img', { ...props, src: typeof src === 'string' ? src : src.src });
}

/** 链接保留真实 DOM 事件与焦点，不构造测试专用虚拟节点。 */
function Link({ children, ...props }) {
  return React.createElement('a', props, children);
}
