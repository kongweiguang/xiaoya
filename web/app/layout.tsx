import type { Metadata } from 'next';
import { ThemeProvider } from '@/components/app/theme-provider';
import '@/styles/globals.css';

export const metadata: Metadata = {
  title: '小芽 · 和你聊聊',
  description: '一个会倾听、会思考的可爱语音伙伴。',
};
/** 中文使用系统字体以兼容离线私有部署，主题可跟随设备或由用户选择。 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
