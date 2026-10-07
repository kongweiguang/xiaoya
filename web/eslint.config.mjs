import { dirname } from 'path';
import { fileURLToPath } from 'url';
import { FlatCompat } from '@eslint/eslintrc';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  {
    ignores: [
      // 独立验证目录同样是 Next 产物，不能把生成代码作为项目源码扫描。
      '.next*/**',
      'next-env.d.ts',
      'lib/avatar/vendor/**',
      'lib/avatar/vendor-source/**',
      'types/live2dcubism*.d.ts',
      'public/avatar/vendor/**',
    ],
  },
  ...compat.extends(
    'next/core-web-vitals',
    'next/typescript',
    'plugin:import/recommended',
    'prettier',
    'plugin:prettier/recommended'
  ),
];

export default eslintConfig;
