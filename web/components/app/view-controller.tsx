'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { ArrowDown, LoaderCircle, MessageSquare, Minus, X } from 'lucide-react';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
import { AvatarStage } from '@/components/app/avatar-stage';
import { ConversationControls } from '@/components/app/conversation-controls';
import { ThemeToggle } from '@/components/app/theme-toggle';
import { Button } from '@/components/ui/button';
import type { Conversation } from '@/hooks/use-conversation';
import { groupReplyMessages } from '@/lib/avatar/delivery';
import avatarPoster from '@/public/avatar/xiaoya/concept-v1/poster.png';

/** 页面持续持有草稿和阅读意图；浮层开合、连接恢复及设备失败均不能重建会话。 */
export function ViewController({ conversation }: { conversation: Conversation }) {
  const { messages, send, sending: isSending, attempt, peer } = conversation;
  const [draft, setDraft] = useState('');
  const [mobile, setMobile] = useState(false);
  const [historyChoice, setHistoryChoice] = useState<boolean | null>(null);
  const [showLatest, setShowLatest] = useState(false);
  const displayMessages = useMemo(() => groupReplyMessages(messages), [messages]);
  // 手机展示当前语义片段，完整回复只在历史合并，避免长回复把正在说的字幕挤出可见区域。
  const latestReply = messages.findLast((message) => !message.from?.isLocal);
  const historyOpen = historyChoice ?? !mobile;
  const scroll = useRef<HTMLDivElement>(null);
  const history = useRef<HTMLElement>(null);
  const lastScroll = useRef(true);
  const app = useRef<HTMLElement>(null);
  /** 同房恢复保留阅读位置；只有新的会话才重新跟随最新回复。 */
  useEffect(() => {
    lastScroll.current = true;
    setShowLatest(false);
  }, [conversation.conversationId]);
  /** 视口订阅仅属于页面；键盘改变可见高度而不改变角色和会话资源的所有权。 */
  useEffect(() => {
    const breakpoint = window.matchMedia('(max-width: 720px)');
    /** 宽度变化只决定浮层默认方式，显式开合由用户选择继续持有。 */
    function updateMobile() {
      setMobile(breakpoint.matches);
    }
    /** 缩放不重排；可见高度比布局视口更早感知手机软键盘。 */
    function resize() {
      const viewport = window.visualViewport;
      if (viewport && viewport.scale !== 1) return;
      const height = viewport?.height ?? window.innerHeight;
      if (!app.current) return;
      app.current.style.setProperty('--app-height', `${height}px`);
      app.current.dataset.compactHeight = String(height < 560);
    }
    updateMobile();
    resize();
    breakpoint.addEventListener('change', updateMobile);
    window.visualViewport?.addEventListener('resize', resize);
    window.addEventListener('resize', resize);
    /** 解除所有布局订阅，结束聊天后的旧页面不能继续修改新视图。 */
    return () => {
      breakpoint.removeEventListener('change', updateMobile);
      window.visualViewport?.removeEventListener('resize', resize);
      window.removeEventListener('resize', resize);
    };
  }, []);
  /** 手机历史是模态抽屉：焦点只能在抽屉内流转，关闭时归还给打开它的控件。 */
  useEffect(() => {
    if (!mobile || !historyOpen || !history.current) return;
    const panel = history.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.querySelector<HTMLButtonElement>('.history-close')?.focus();
    /** Escape 关闭、Tab 循环；不拦截聊天记录区域的普通阅读按键。 */
    function manageFocus(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        setHistoryChoice(false);
      } else if (event.key === 'Tab') {
        const controls = Array.from(
          panel.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex="0"]')
        );
        const first = controls[0];
        const last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener('keydown', manageFocus);
    /** 抽屉隐藏后恢复原入口；断开时已消失的控件不能成为焦点目标。 */
    return () => {
      document.removeEventListener('keydown', manageFocus);
      if (previous?.isConnected) previous.focus();
    };
  }, [mobile, historyOpen]);
  /** 桌面浮层收起后把焦点移到重新打开的入口，隐藏的关闭按钮不能滞留键盘焦点。 */
  useEffect(() => {
    if (!mobile && !historyOpen)
      app.current
        ?.querySelector<HTMLButtonElement>('.header-actions button[aria-label="查看聊天记录"]')
        ?.focus();
  }, [mobile, historyOpen]);
  const connected = conversation.phase === 'active' || conversation.phase === 'recovering';
  const reconnecting = conversation.phase === 'recovering';
  const connecting = conversation.phase === 'connecting';
  const status = connecting
    ? '正在连接，马上就好…'
    : conversation.phase === 'ending'
      ? '正在结束聊天…'
      : reconnecting
        ? '正在恢复连接…'
        : !connected
          ? '在这里，等你来聊'
          : peer.state === 'speaking'
            ? '正在和你说话'
            : peer.state === 'thinking'
              ? '让我想一想…'
              : conversation.microphoneCapturing
                ? '正在听你说'
                : '等你发来消息';
  /** 内容到达只在用户仍跟随最新时滚动，历史阅读不会被流式回复打断。 */
  useEffect(() => {
    if (lastScroll.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages]);
  /** 容器高度和内容更新分别观察，长草稿、软键盘与历史开合都保留阅读位置。 */
  useEffect(() => {
    const node = scroll.current;
    if (!node) return;
    /** 布局通知不能改变阅读意图，仅在原本跟随最新时维持底部。 */
    function keepLatestReplyVisible() {
      if (lastScroll.current && node) node.scrollTop = node.scrollHeight;
    }
    const observer = new ResizeObserver(keepLatestReplyVisible);
    observer.observe(node);
    /** 卸载时解除唯一字幕观察器，避免旧尺寸通知抢占新的滚动位置。 */
    return () => observer.disconnect();
  }, []);
  /** 开合历史只改变表现方式，保持草稿、消息节点和滚动位置。 */
  function toggleHistory() {
    setHistoryChoice(!historyOpen);
  }
  /** 用户主动回到最新后恢复跟随；避免每次收到回复都强制执行这个动作。 */
  function returnToLatest() {
    lastScroll.current = true;
    setShowLatest(false);
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }
  /** 滚动距离只在边界变化时更新 UI，逐像素滚动不会造成重复重渲染。 */
  function recordReadingPosition() {
    const node = scroll.current;
    if (!node) return;
    const following = node.scrollHeight - node.scrollTop - node.clientHeight < 70;
    lastScroll.current = following;
    setShowLatest(!following);
  }
  return (
    <main
      className="xiaoya-app"
      ref={app}
      data-history-open={historyOpen}
      data-history-choice={historyChoice === null ? 'auto' : 'explicit'}
      data-connected={connected}
    >
      <header className="app-header" inert={mobile && historyOpen ? true : undefined}>
        <Link href="/" className="app-brand" aria-label="小芽首页">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none" aria-hidden="true">
            <path d="M20 22C7 24 3 17 3 10c10-2 18 1 17 12Z" fill="#83a96a" />
            <path d="M20 21C19 8 28 4 38 6c1 10-5 16-18 15Z" fill="#8cb171" />
            <path d="M20 20c1 7-1 12-3 17" stroke="#64894f" strokeWidth="3" strokeLinecap="round" />
          </svg>
          <span>小芽</span>
        </Link>
        <div className="header-actions">
          {!historyOpen && !mobile && (
            <Button variant="ghost" size="icon" aria-label="查看聊天记录" onClick={toggleHistory}>
              <MessageSquare size={20} />
            </Button>
          )}
          <ThemeToggle />
        </div>
      </header>
      <div className="conversation-layout">
        <div className="stage-region" inert={mobile && historyOpen ? true : undefined}>
          <AvatarStage
            connected={connected}
            reconnecting={reconnecting}
            status={status}
            error={!!conversation.error}
            messages={messages}
            room={attempt?.room}
            audio={attempt?.audio ?? null}
            peer={peer}
            delivery={conversation.controller.delivery}
          />
          {mobile && (
            <p className="current-caption" aria-live={historyOpen ? 'off' : 'polite'}>
              {latestReply?.message ||
                (connected ? '想聊什么，都可以告诉我。' : '你好呀，很高兴认识你。')}
            </p>
          )}
        </div>
        {mobile && historyOpen && (
          <button
            className="history-backdrop"
            aria-label="关闭聊天记录"
            tabIndex={-1}
            onClick={toggleHistory}
          />
        )}
        <section
          id="chat-history"
          className="transcript-panel"
          aria-label={mobile ? '聊天记录抽屉' : '对话字幕'}
          role={mobile && historyOpen ? 'dialog' : undefined}
          aria-modal={mobile && historyOpen ? true : undefined}
          aria-hidden={!historyOpen}
          inert={!historyOpen ? true : undefined}
          ref={history}
        >
          <div className="transcript-heading">
            <h2>对话</h2>
            <Button
              variant="ghost"
              size="icon"
              className="history-close"
              aria-label="收起聊天记录"
              onClick={toggleHistory}
            >
              {mobile ? <X size={21} /> : <Minus size={21} />}
            </Button>
          </div>
          <div
            className="transcript-scroll"
            ref={scroll}
            role="log"
            tabIndex={0}
            aria-label="聊天记录"
            aria-live="polite"
            aria-relevant="additions text"
            onScroll={recordReadingPosition}
          >
            {!messages.length && (
              <div className="transcript-empty">
                <MessageSquare size={32} strokeWidth={1.3} />
                <h3>你好呀，很高兴认识你</h3>
                <p>聊聊今天的心情，或分享一件有趣的小事。</p>
              </div>
            )}
            {displayMessages.map(({ id, timestamp, from, message }) => (
              <div className={from?.isLocal ? 'chat-row user' : 'chat-row assistant'} key={id}>
                {!from?.isLocal && (
                  <Image src={avatarPoster} width={38} height={38} alt="" className="chat-avatar" />
                )}
                <div>
                  {from?.isLocal && (
                    <time>
                      {new Date(timestamp).toLocaleTimeString('zh-CN', {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </time>
                  )}
                  <p className="chat-bubble">{message}</p>
                  {!from?.isLocal && (
                    <time>
                      {new Date(timestamp).toLocaleTimeString('zh-CN', {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </time>
                  )}
                </div>
              </div>
            ))}
            {connected && !reconnecting && peer.state === 'thinking' && (
              <p className="thinking-indicator" role="status">
                <span />
                小芽正在想一想…
              </p>
            )}
          </div>
          {showLatest && (
            <Button variant="outline" className="return-latest" onClick={returnToLatest}>
              <ArrowDown size={15} />
              回到最新
            </Button>
          )}
        </section>
      </div>
      <div className="dock-region" inert={mobile && historyOpen ? true : undefined}>
        {conversation.error && (
          <div className="connection-error" role="alert">
            <p>{conversation.error}</p>
          </div>
        )}
        {connected && attempt ? (
          <ConversationControls
            key={conversation.conversationId}
            draft={draft}
            setDraft={setDraft}
            ready={!reconnecting}
            room={attempt.room}
            send={send}
            sending={isSending}
            end={conversation.end}
            onMicrophoneChange={attempt.reportMicrophone}
            historyOpen={historyOpen}
            onHistoryToggle={toggleHistory}
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
            {mobile && (
              <Button variant="ghost" size="icon" aria-label="查看聊天记录" onClick={toggleHistory}>
                <MessageSquare size={20} />
              </Button>
            )}
          </div>
        )}
        {connected && attempt && (
          <StartAudioButton room={attempt.room} label="点击开启声音" className="start-audio" />
        )}
        <footer className="app-footer">
          {connecting
            ? '正在连接，可以随时取消。'
            : conversation.phase === 'ending'
              ? '正在结束聊天，未发送的文字会保留。'
              : reconnecting
                ? '正在恢复连接，未发送的文字会保留。'
                : connected
                  ? conversation.microphoneCapturing
                    ? '随时开口，也可以打字。说话时可以打断我。'
                    : '可以打字，也可以开启麦克风。'
                  : '一个会倾听、会思考的小伙伴。'}
        </footer>
      </div>
    </main>
  );
}
