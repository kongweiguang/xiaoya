'use client';

import { useEffect, useRef } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { LoaderCircle, MessageCircle, Sprout } from 'lucide-react';
import { useSessionMessages } from '@livekit/components-react';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
import { AvatarStage } from '@/components/app/avatar-stage';
import { ConversationControls } from '@/components/app/conversation-controls';
import { ThemeToggle } from '@/components/app/theme-toggle';
import { Button } from '@/components/ui/button';
import { useConversation } from '@/hooks/use-conversation';
import { groupReplyMessages } from '@/lib/avatar/delivery';
import avatarPoster from '@/public/avatar/xiaoya/poster.png';

/** 舞台与字幕共用真实会话及带内容指纹的形象；键盘出现时保留对话和操作，等待及错误都有退出入口。 */
export function ViewController({ resetRoom }: { resetRoom: () => void }) {
  const conversation = useConversation(resetRoom);
  const { messages, send, isSending } = useSessionMessages();
  const displayMessages = groupReplyMessages(messages);
  const scroll = useRef<HTMLDivElement>(null);
  const lastScroll = useRef(true);
  const app = useRef<HTMLElement>(null);
  /** 只在挂载期间追踪视口，卸载解除监听，避免反复会话积累布局事件。 */
  useEffect(() => {
    /** 按可见高度收起装饰，让软键盘和长草稿同时出现时仍能阅读回复；缩放不触发重排。 */
    function resize() {
      const viewport = window.visualViewport;
      if (viewport && viewport.scale !== 1) return;
      const height = viewport?.height ?? window.innerHeight;
      if (!app.current) return;
      app.current.style.setProperty('--app-height', `${height}px`);
      app.current.dataset.compactHeight = String(height < 560);
    }
    resize();
    window.visualViewport?.addEventListener('resize', resize);
    window.addEventListener('resize', resize);
    /** 同时解除两种尺寸事件，使旧视图不会在新会话更新可见高度。 */
    return () => {
      window.visualViewport?.removeEventListener('resize', resize);
      window.removeEventListener('resize', resize);
    };
  }, []);
  const connected = conversation.phase === 'active';
  const connecting = conversation.phase === 'connecting';
  const status = connecting
    ? '正在连接，马上就好…'
    : conversation.phase === 'ending'
      ? '正在结束聊天…'
      : conversation.reconnecting
        ? '正在恢复连接…'
        : !connected
          ? '在这里，等你开口'
          : conversation.agent.state === 'speaking'
            ? '正在和你说话'
            : conversation.agent.state === 'thinking'
              ? '让我想一想…'
              : '正在听你说';
  useEffect(() => {
    if (lastScroll.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages]);
  /** 内容变化与容器变化分开观察，键盘或草稿重排时也保留阅读位置。 */
  useEffect(() => {
    const node = scroll.current;
    if (!node) return;
    /** 草稿变高或键盘缩小字幕区时保留最新回复；用户主动翻阅历史时不抢回滚动位置。 */
    function keepLatestReplyVisible() {
      if (lastScroll.current && node) node.scrollTop = node.scrollHeight;
    }
    const observer = new ResizeObserver(keepLatestReplyVisible);
    observer.observe(node);
    /** 观察器仅属于当前页面，不让卸载后的尺寸事件抢占新视图滚动。 */
    return () => observer.disconnect();
  }, []);
  return (
    <main className="xiaoya-app" ref={app}>
      <header className="app-header">
        <Link href="/" className="app-brand" aria-label="小芽首页">
          <Sprout size={31} strokeWidth={2.4} />
          <span>小芽</span>
        </Link>
        <ThemeToggle />
      </header>
      <div className="conversation-layout">
        <AvatarStage
          connected={connected}
          reconnecting={conversation.reconnecting}
          status={status}
          error={!!conversation.error}
          messages={messages}
        />
        <section className="transcript-panel" aria-label="对话字幕">
          <div className="transcript-heading">
            <h2>对话</h2>
            <span>{connected ? '正在聊天' : '和你聊聊'}</span>
          </div>
          <div
            className="transcript-scroll"
            ref={scroll}
            role="log"
            aria-label="聊天记录"
            aria-live="polite"
            aria-relevant="additions text"
            onScroll={() => {
              const node = scroll.current;
              if (node)
                lastScroll.current = node.scrollHeight - node.scrollTop - node.clientHeight < 70;
            }}
          >
            {!messages.length && (
              <div className="transcript-empty">
                <MessageCircle size={36} strokeWidth={1.4} />
                <h3>你好呀，很高兴认识你</h3>
                <p>
                  聊聊今天的心情，问一个问题，
                  <br />
                  或分享一件有趣的小事。
                </p>
                <span>你的话，我都在听。</span>
              </div>
            )}
            {displayMessages.map(({ id, timestamp, from, message }) => (
              <div className={from?.isLocal ? 'chat-row user' : 'chat-row assistant'} key={id}>
                {!from?.isLocal && (
                  <Image src={avatarPoster} width={38} height={38} alt="" className="chat-avatar" />
                )}
                <div>
                  <p className="chat-bubble">{message}</p>
                  <time>
                    {new Date(timestamp).toLocaleTimeString('zh-CN', {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </time>
                </div>
              </div>
            ))}
            {connected && conversation.agent.state === 'thinking' && (
              <p className="thinking-indicator" role="status">
                <span />
                小芽正在想一想…
              </p>
            )}
          </div>
        </section>
      </div>
      {conversation.error && (
        <div className="connection-error" role="alert">
          <p>{conversation.error}</p>
        </div>
      )}
      {connected ? (
        <ConversationControls
          key={conversation.conversationId}
          ready={!conversation.reconnecting}
          send={send}
          sending={isSending}
          end={conversation.end}
        />
      ) : (
        <div className="welcome-controls">
          {connecting ? (
            <>
              <Button disabled className="start-button">
                <LoaderCircle className="spin" size={19} />
                正在连接
              </Button>
              <Button variant="outline" onClick={() => void conversation.end()}>
                取消连接
              </Button>
            </>
          ) : (
            <>
              <Button
                className="start-button"
                disabled={conversation.phase === 'ending'}
                onClick={() => void conversation.start(true)}
              >
                {conversation.phase === 'ending'
                  ? '正在结束…'
                  : conversation.error
                    ? '重新连接'
                    : '开始聊天'}
              </Button>
              <Button
                variant="ghost"
                disabled={conversation.phase === 'ending'}
                onClick={() => void conversation.start(false)}
              >
                {conversation.microphoneFailed ? '改用文字聊天' : '用文字聊聊'}
              </Button>
            </>
          )}
        </div>
      )}
      {connected && <StartAudioButton label="点击开启声音" className="start-audio" />}
      <footer className="app-footer">
        {connected ? '随时开口，也可以打字。说话时可以打断我。' : '一个会倾听、会思考的小伙伴。'}
        <span>{connected ? 'Enter 发送 · Shift + Enter 换行' : '也可以直接用文字聊天。'}</span>
      </footer>
    </main>
  );
}
