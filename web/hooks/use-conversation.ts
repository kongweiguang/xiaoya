'use client';

import { useEffect, useState, useSyncExternalStore } from 'react';
import { ConversationController } from '@/lib/conversation-controller';

export { microphoneError } from '@/lib/conversation-controller';

/** 页面只订阅一个控制器；挂载不申请媒体，StrictMode 与真正卸载共享资源回收边界。 */
export function useConversation() {
  const [controller] = useState(() => new ConversationController());
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot
  );
  useEffect(() => controller.mount(), [controller]);
  return {
    ...snapshot,
    controller,
    start: controller.start,
    end: controller.end,
    send: controller.send,
  };
}
export type Conversation = ReturnType<typeof useConversation>;
