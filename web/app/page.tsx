import { App } from '@/components/app/app';

/** 私有部署固定使用本项目的令牌接口，避免配置缺失时连接官网演示 Agent。 */
export default function Page() {
  return <App tokenEndpoint="/api/token" agentName={process.env.AGENT_NAME} />;
}
