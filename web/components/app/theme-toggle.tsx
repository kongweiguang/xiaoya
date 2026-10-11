'use client';

import { useEffect, useState } from 'react';
import { useTheme } from 'next-themes';
import { Moon, Sun } from 'lucide-react';
import { Button } from '@/components/ui/button';

/** 主题按钮始终可见；挂载后才读设备主题以避免服务端和浏览器渲染不一致。 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  /** 系统主题由 ThemeProvider 自动解析；水合后再标记选中，防止首屏内容不一致。 */
  useEffect(() => setMounted(true), []);
  return (
    <div className="theme-switch" aria-label="界面主题">
      {[
        { value: 'light', label: '浅色主题', icon: Sun },
        { value: 'dark', label: '深色主题', icon: Moon },
      ].map(({ value, label, icon: Icon }) => (
        <Button
          key={value}
          variant="ghost"
          size="icon"
          aria-label={label}
          title={label}
          aria-pressed={mounted && resolvedTheme === value}
          onClick={() => setTheme(value)}
        >
          <Icon size={20} />
        </Button>
      ))}
    </div>
  );
}
