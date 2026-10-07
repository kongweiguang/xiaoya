import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // 本机日常使用开发服务，工具浮标会遮住窄屏麦克风操作；诊断仍由终端保留。
  devIndicators: false,
  // 构建验证使用独立目录，避免覆盖正在运行的开发服务缓存。
  distDir: process.env.NEXT_DIST_DIR || '.next',
};

export default nextConfig;
