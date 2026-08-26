export type User = {
  id: string;
  name: string;
  phone: string;
};

export type Device = {
  id: string;
  name: string;
  serial: string;
  status: 'online' | 'offline';
  version: string;
  lastSeenAt: string;
  agentProvider?: AgentProvider;
  agentModel?: string;
  systemStatus?: DeviceSystemStatus;
};

export type DeviceSystemStatus = {
  online: boolean;
  cpuPercent: number | null;
  memoryPercent: number | null;
  memoryUsedBytes: number | null;
  memoryTotalBytes: number | null;
  diskPercent: number | null;
  diskUsedBytes: number | null;
  diskTotalBytes: number | null;
  sampledAt: string;
};

export type AuthSession = {
  token: string;
  user: User;
  devices: Device[];
};

export type AgentMessage = {
  id: string;
  taskId?: string;
  role: 'assistant' | 'user';
  text: string;
  createdAt: string;
  attachments?: ChatAttachment[];
  error?: string;
  interaction?: AgentInteraction;
  status?: string;
  steps?: AgentStep[];
  streaming?: boolean;
};

export type ChatAttachment = {
  id: string;
  name: string;
  mimeType: string;
  size: number;
  uri?: string;
};

export type ChatAttachmentUpload = ChatAttachment & {
  data: string;
};

export type ChatAttachmentTransfer = ChatAttachment & {
  data: string;
};

export type AgentStreamMessage = Omit<AgentMessage, 'attachments'> & {
  attachments?: ChatAttachmentTransfer[];
};

export type AgentStep = {
  id: string;
  kind?: 'text' | 'tool';
  label: string;
  state: 'running' | 'done' | 'error' | 'cancelled';
};

export type AgentCommand = {
  name: string;
  description: string;
  source: 'extension' | 'prompt' | 'skill';
};

export type AgentInteraction = {
  id: string;
  method: 'select' | 'confirm' | 'input' | 'editor';
  title: string;
  message: string;
  options: string[];
  placeholder: string;
  pending: boolean;
  answer?: string;
};

export type InteractionResponse = {
  value?: string;
  confirmed?: boolean;
  cancelled?: boolean;
};

export type AgentStreamHandlers = {
  onDelta: (delta: string) => void;
  onText?: (text: string) => void;
  onInteraction: (
    interaction: AgentInteraction,
    respond: (response: InteractionResponse) => Promise<void>,
  ) => void;
  onStatus: (step: AgentStep) => void;
};

type AgentTaskSnapshot = {
  taskId: string;
  status: 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled';
  text: string;
  events: Record<string, unknown>[];
  interaction?: Record<string, unknown> | null;
  message?: AgentStreamMessage | null;
  error?: string | null;
};

export type Conversation = {
  id: string;
  title: string;
  deviceId: string;
  updatedAt: string;
  messages: AgentMessage[];
};

export function normalizeConversations(conversations: Conversation[]): Conversation[] {
  if (!Array.isArray(conversations)) return [];
  return conversations
    .filter((conversation) => (
      !!conversation?.id
      && !!conversation.deviceId
      && !!conversation.updatedAt
      && Array.isArray(conversation.messages)
    ))
    .map((conversation) => {
      const seen = new Set<string>();
      const messages: AgentMessage[] = [];
      for (let index = conversation.messages.length - 1; index >= 0; index -= 1) {
        const message = conversation.messages[index];
        if (!message?.id || seen.has(message.id)) continue;
        seen.add(message.id);
        messages.push(message);
      }
      messages.reverse();
      return { ...conversation, messages };
    });
}

export type AgentProvider = string;

export type AgentProviderOption = {
  id: AgentProvider;
  label: string;
};

export type AgentModelOption = {
  id: string;
  name: string;
  reasoning: boolean;
  contextWindow: number;
};

export type AgentConfiguration = {
  configured: boolean;
  provider: AgentProvider | '';
  model: string;
  providers: AgentProviderOption[];
  models: AgentModelOption[];
};

export type DeviceCapability = {
  id: string;
  name: string;
  kind: 'skill' | 'extension' | 'mcp';
  description: string;
  version: string;
  source: string;
  permissions: string[];
  enabled: boolean;
  artifactAvailable: boolean;
  installed: boolean;
  installedVersion: string;
  local: boolean;
  managed: boolean;
};

export type WifiNetwork = {
  ssid: string;
  signal: number;
  secured: boolean;
};

export type DeviceProvisioning = {
  claimToken: string;
  existingDeviceIds?: string[];
  existingDevices: Pick<Device, 'id' | 'name' | 'status' | 'lastSeenAt'>[];
  expiresAt: string;
  name: string;
};

type WifiNetworksResponse = {
  networks?: WifiNetwork[];
  refreshing?: boolean;
  refreshError?: string;
};

type SessionPayload = Omit<AuthSession, 'devices'> & {
  devices?: Device[];
  device?: Device | null;
};

const API_URL = process.env.EXPO_PUBLIC_API_URL?.replace(/\/$/, '');
const HOST_SETUP_URL =
  process.env.EXPO_PUBLIC_HOST_SETUP_URL?.replace(/\/$/, '') || 'http://192.168.4.1:8090';
export const isDemoMode = !API_URL;

class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'ApiError';
  }
}

export class AgentCancelledError extends Error {
  constructor() {
    super('已停止运行');
    this.name = 'AgentCancelledError';
  }
}

function delay(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function requestUrl<T>(
  url: string,
  init: RequestInit,
  token?: string,
  timeout = 15_000,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        body?.detail || body?.message || `请求失败（${response.status}）`,
        response.status,
      );
    }
    return body as T;
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('连接超时，请检查网络后重试');
    }
    throw error instanceof Error ? error : new Error('网络连接失败');
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(
  path: string,
  init: RequestInit,
  token?: string,
  timeout?: number,
): Promise<T> {
  return requestUrl<T>(`${API_URL}${path}`, init, token, timeout);
}

function normalizeSession(value: SessionPayload): AuthSession {
  if (!value?.token || !value.user?.id || typeof value.user.phone !== 'string') {
    throw new Error('登录服务返回了无效数据');
  }
  return {
    token: value.token,
    user: value.user,
    devices: Array.isArray(value.devices) ? value.devices : value.device ? [value.device] : [],
  };
}

export async function login(phone: string, password: string): Promise<AuthSession> {
  if (isDemoMode) {
    await delay(450);
    return {
      token: 'demo-token',
      user: { id: 'demo-user', name: '主机用户', phone },
      devices: [],
    };
  }
  const session = await request<SessionPayload>('/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ phone, password }),
  });
  return normalizeSession(session);
}

export async function register(name: string, phone: string, password: string): Promise<AuthSession> {
  if (isDemoMode) {
    await delay(500);
    return {
      token: 'demo-token',
      user: { id: 'demo-user', name, phone },
      devices: [],
    };
  }
  const session = await request<SessionPayload>('/v1/auth/register', {
    method: 'POST',
    body: JSON.stringify({ name, phone, password }),
  });
  return normalizeSession(session);
}

function wifiNetworks(response: WifiNetworksResponse): WifiNetwork[] {
  if (!Array.isArray(response.networks)) throw new Error('主机返回了无效的 Wi-Fi 列表');
  return response.networks.filter(
    (network) => network?.ssid && Number.isFinite(network.signal),
  );
}

export async function scanHostWifiNetworks(): Promise<WifiNetwork[]> {
  if (isDemoMode) {
    await delay(450);
    return [
      { ssid: 'Home WiFi', signal: 88, secured: true },
      { ssid: 'Guest', signal: 61, secured: false },
    ];
  }
  const response = await requestUrl<WifiNetworksResponse>(
    `${HOST_SETUP_URL}/wifi-networks`,
    { method: 'GET' },
    undefined,
    30_000,
  );
  return wifiNetworks(response);
}

export async function refreshHostWifiNetworks(): Promise<WifiNetwork[]> {
  if (isDemoMode) return scanHostWifiNetworks();
  await requestUrl(
    `${HOST_SETUP_URL}/wifi-networks/refresh`,
    { method: 'POST' },
    undefined,
    5_000,
  );

  let lastError: unknown;
  for (let attempt = 0; attempt < 15; attempt += 1) {
    await delay(2_000);
    try {
      const response = await requestUrl<WifiNetworksResponse>(
        `${HOST_SETUP_URL}/wifi-networks`,
        { method: 'GET' },
        undefined,
        3_000,
      );
      if (response.refreshing) continue;
      if (response.refreshError) throw new Error(response.refreshError);
      return wifiNetworks(response);
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`热点恢复超时：${lastError instanceof Error ? lastError.message : '请重新连接主机热点'}`);
}

export async function prepareDeviceProvisioning(
  token: string,
  name: string,
): Promise<DeviceProvisioning> {
  if (isDemoMode) {
    return {
      claimToken: 'demo-claim-token',
      existingDevices: [],
      expiresAt: new Date(Date.now() + 10 * 60_000).toISOString(),
      name,
    };
  }
  const existingDevices = await request<Device[]>('/v1/devices', { method: 'GET' }, token);
  const provisioning = await request<{ claimToken: string; expiresAt: string }>(
    '/v1/provisioning/sessions',
    { method: 'POST', body: JSON.stringify({ name }) },
    token,
  );
  return {
    claimToken: provisioning.claimToken,
    existingDevices: existingDevices.map(({ id, name: deviceName, status, lastSeenAt }) => ({
      id,
      name: deviceName,
      status,
      lastSeenAt,
    })),
    expiresAt: provisioning.expiresAt,
    name,
  };
}

export async function configureDeviceNetwork(
  token: string,
  provisioning: DeviceProvisioning,
  wifiName: string,
  wifiPassword: string,
): Promise<Device> {
  if (isDemoMode) {
    await delay(850);
    const suffix = Date.now().toString().slice(-6);
    return {
      id: `demo-host-${suffix}`,
      name: provisioning.name,
      serial: `CP-${suffix}`,
      status: 'online',
      version: 'ClawPi OS 1.0.0',
      lastSeenAt: new Date().toISOString(),
    };
  }
  if (Date.parse(provisioning.expiresAt) <= Date.now()) {
    throw new Error('绑定准备已过期，请重新连接互联网后再试');
  }
  const previousDevices = new Map(
    (provisioning.existingDevices ?? []).map((device) => [device.id, device]),
  );
  const existingIds = new Set(
    provisioning.existingDevices?.map((device) => device.id) ?? provisioning.existingDeviceIds ?? [],
  );
  const configured = await requestUrl<{ accepted?: boolean; device?: Device }>(
    `${HOST_SETUP_URL}/provision`,
    {
      method: 'POST',
      body: JSON.stringify({
        claimToken: provisioning.claimToken,
        cloudUrl: API_URL,
        wifiName,
        wifiPassword,
      }),
    },
    undefined,
    15_000,
  );
  if (!configured.accepted && (!configured.device?.id || !configured.device.serial)) {
    throw new Error('主机返回了无效的配网信息');
  }

  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    try {
      const devices = await requestUrl<Device[]>(
        `${API_URL}/v1/devices`,
        { method: 'GET' },
        token,
        5_000,
      );
      const device = configured.device?.id
        ? devices.find((item) => item.id === configured.device?.id)
        : devices.find((item) => !existingIds.has(item.id)) ?? devices.find((item) => {
            const previous = previousDevices.get(item.id);
            return previous
              && item.name === provisioning.name
              && (item.lastSeenAt !== previous.lastSeenAt || item.status !== previous.status);
          });
      if (device?.status === 'online') return device;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) throw error;
      // The phone briefly loses networking while the host hotspot shuts down.
    }
    await delay(1_000);
  }
  if (configured.device) return configured.device;
  throw new Error('主机联网超时，请确认家庭 Wi-Fi 可用后重试');
}

export async function configureDeviceAgent(
  token: string,
  deviceId: string,
  provider: AgentProvider,
  apiKey: string | undefined,
  model: string,
): Promise<void> {
  if (isDemoMode) {
    await delay(650);
    return;
  }
  await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/agent-config`,
    {
      method: 'POST',
      body: JSON.stringify({
        provider,
        apiKey: apiKey?.trim() || undefined,
        model: model.trim() || undefined,
      }),
    },
    token,
  );
}

export async function getDeviceAgentConfig(
  token: string,
  deviceId: string,
): Promise<AgentConfiguration> {
  if (isDemoMode) {
    await delay(300);
    return {
      configured: false,
      provider: '',
      model: '',
      providers: [
        { id: 'openai', label: 'OpenAI' },
        { id: 'anthropic', label: 'Anthropic' },
        { id: 'google', label: 'Google Gemini' },
      ],
      models: [],
    };
  }
  return request<AgentConfiguration>(
    `/v1/devices/${encodeURIComponent(deviceId)}/agent-config`,
    { method: 'GET' },
    token,
  );
}

export async function getDevice(token: string, deviceId: string): Promise<Device> {
  if (isDemoMode) {
    await delay(350);
    return {
      id: deviceId,
      name: '我的 AI 主机',
      serial: `CP-${deviceId.slice(-6).toUpperCase()}`,
      status: 'online',
      version: 'ClawPi OS 1.0.0',
      lastSeenAt: new Date().toISOString(),
    };
  }
  return request<Device>(`/v1/devices/${encodeURIComponent(deviceId)}`, { method: 'GET' }, token);
}

export async function getDevices(token: string): Promise<Device[]> {
  if (isDemoMode) {
    await delay(300);
    return [];
  }
  return request<Device[]>('/v1/devices', { method: 'GET' }, token);
}

export async function getDeviceConversations(
  token: string,
  deviceId: string,
): Promise<Conversation[]> {
  if (isDemoMode) return [];
  const conversations = await request<Conversation[]>(
    `/v1/devices/${encodeURIComponent(deviceId)}/conversations`,
    { method: 'GET' },
    token,
    30_000,
  );
  return normalizeConversations(conversations);
}

export async function syncDeviceConversations(
  token: string,
  deviceId: string,
  conversations: Conversation[],
): Promise<Conversation[]> {
  if (isDemoMode) return normalizeConversations(conversations);
  const synced = await request<Conversation[]>(
    `/v1/devices/${encodeURIComponent(deviceId)}/conversations`,
    {
      method: 'PUT',
      body: JSON.stringify({ conversations }),
    },
    token,
    30_000,
  );
  return normalizeConversations(synced);
}

export async function deleteDeviceConversation(
  token: string,
  deviceId: string,
  conversationId: string,
): Promise<void> {
  if (isDemoMode) return;
  await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/conversations/${encodeURIComponent(conversationId)}`,
    { method: 'DELETE' },
    token,
    30_000,
  );
}

export async function getDeviceSystemStatus(
  token: string,
  deviceId: string,
): Promise<DeviceSystemStatus> {
  if (isDemoMode) {
    await delay(250);
    return {
      online: true,
      cpuPercent: 18.5,
      memoryPercent: 42,
      memoryUsedBytes: 3_500_000_000,
      memoryTotalBytes: 8_000_000_000,
      diskPercent: 61.5,
      diskUsedBytes: 78_000_000_000,
      diskTotalBytes: 128_000_000_000,
      sampledAt: new Date().toISOString(),
    };
  }
  return request<DeviceSystemStatus>(
    `/v1/devices/${encodeURIComponent(deviceId)}/system-status`,
    { method: 'GET' },
    token,
  );
}

export async function getDeviceCapabilities(
  token: string,
  deviceId: string,
): Promise<DeviceCapability[]> {
  if (isDemoMode) {
    await delay(350);
    return [
      {
        id: 'web-search',
        name: '网页搜索',
        kind: 'skill',
        description: '让 Agent 能够检索并整理公开网页信息。',
        version: '1.0.0',
        source: '',
        permissions: ['网络访问'],
        enabled: true,
        artifactAvailable: true,
        installed: false,
        installedVersion: '',
        local: false,
        managed: true,
      },
      {
        id: 'local-extension:workspace-tools',
        name: 'workspace-tools',
        kind: 'extension',
        description: '在主机本地发现，未由能力商店管理。',
        version: '',
        source: '',
        permissions: [],
        enabled: false,
        artifactAvailable: false,
        installed: true,
        installedVersion: '',
        local: true,
        managed: false,
      },
    ];
  }
  return request<DeviceCapability[]>(
    `/v1/devices/${encodeURIComponent(deviceId)}/capabilities`,
    { method: 'GET' },
    token,
    30_000,
  );
}

export async function installDeviceCapability(
  token: string,
  deviceId: string,
  capabilityId: string,
): Promise<void> {
  if (isDemoMode) {
    await delay(800);
    return;
  }
  await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/capabilities/${encodeURIComponent(capabilityId)}`,
    { method: 'POST' },
    token,
    130_000,
  );
}

export async function removeDeviceCapability(
  token: string,
  deviceId: string,
  capabilityId: string,
): Promise<void> {
  if (isDemoMode) {
    await delay(500);
    return;
  }
  await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/capabilities/${encodeURIComponent(capabilityId)}`,
    { method: 'DELETE' },
    token,
    130_000,
  );
}

export async function releaseDevice(token: string, deviceId: string) {
  if (isDemoMode) {
    await delay(300);
    return;
  }
  await request(
    `/v1/devices/${encodeURIComponent(deviceId)}/claim`,
    { method: 'DELETE' },
    token,
  );
}

export async function getDeviceCommands(
  token: string,
  deviceId: string,
): Promise<AgentCommand[]> {
  if (isDemoMode) {
    return [
      { name: 'skill:writer', description: '调用写作 Skill', source: 'skill' },
      { name: 'weather', description: '查询天气', source: 'extension' },
    ];
  }
  return request<AgentCommand[]>(
    `/v1/devices/${encodeURIComponent(deviceId)}/commands`,
    { method: 'GET' },
    token,
    35_000,
  );
}

export async function streamAgentMessage(
  token: string,
  deviceId: string,
  conversationId: string,
  text: string,
  attachments: ChatAttachmentUpload[],
  conversationTitle: string,
  clientMessageId: string,
  createdAt: string,
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<AgentStreamMessage> {
  if (isDemoMode) {
    handlers.onStatus({ id: 'demo', label: '正在整理回复', state: 'running' });
    await delay(350);
    handlers.onText?.('已收到你的消息。');
    handlers.onStatus({ id: 'demo', label: '回复已生成', state: 'done' });
    return {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      text: '已收到你的消息。',
      createdAt: new Date().toISOString(),
    };
  }

  const taskId = clientMessageId;
  if (signal?.aborted) throw new AgentCancelledError();
  try {
    await request<AgentTaskSnapshot>(
      `/v1/devices/${encodeURIComponent(deviceId)}/chat-tasks`,
      {
        method: 'POST',
        body: JSON.stringify({
          taskId,
          clientMessageId,
          conversationId,
          conversationTitle,
          createdAt,
          text,
          attachments,
        }),
      },
      token,
      30_000,
    );
  } catch (startError) {
    if (startError instanceof ApiError) throw startError;
    let recovered = false;
    for (let attempt = 0; attempt < 10 && !signal?.aborted; attempt += 1) {
      try {
        await request<AgentTaskSnapshot>(
          `/v1/chat-tasks/${encodeURIComponent(taskId)}`,
          { method: 'GET' },
          token,
          5_000,
        );
        recovered = true;
        break;
      } catch (recoveryError) {
        if (recoveryError instanceof ApiError) throw startError;
        await delay(1_000);
      }
    }
    if (!recovered) throw startError;
  }
  if (signal?.aborted) {
    await request<AgentTaskSnapshot>(
      `/v1/chat-tasks/${encodeURIComponent(taskId)}`,
      { method: 'DELETE' },
      token,
    ).catch(() => undefined);
    throw new AgentCancelledError();
  }
  return watchAgentTask(token, taskId, handlers, signal);
}

export async function resumeAgentMessage(
  token: string,
  taskId: string,
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<AgentStreamMessage> {
  if (isDemoMode) throw new Error('演示模式没有可恢复的任务');
  return watchAgentTask(token, taskId, handlers, signal);
}

async function watchAgentTask(
  token: string,
  taskId: string,
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<AgentStreamMessage> {
  let previousText = '';
  let interactionId = '';

  while (true) {
    if (signal?.aborted) {
      await request<AgentTaskSnapshot>(
        `/v1/chat-tasks/${encodeURIComponent(taskId)}`,
        { method: 'DELETE' },
        token,
      ).catch(() => undefined);
      throw new AgentCancelledError();
    }

    let task: AgentTaskSnapshot;
    try {
      task = await request<AgentTaskSnapshot>(
        `/v1/chat-tasks/${encodeURIComponent(taskId)}`,
        { method: 'GET' },
        token,
        15_000,
      );
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 404)) throw error;
      await delay(2_000);
      continue;
    }

    const nextText = String(task.text || '');
    if (nextText !== previousText) {
      if (handlers.onText) handlers.onText(nextText);
      else if (nextText.startsWith(previousText)) handlers.onDelta(nextText.slice(previousText.length));
      previousText = nextText;
    }
    for (const event of Array.isArray(task.events) ? task.events : []) {
      if (event.type === 'chat.progress') {
        const label = String(event.text || '').trim();
        if (label) {
          handlers.onStatus({
            id: String(event.progressId || `progress-${Date.now()}`),
            kind: 'text',
            label,
            state: 'done',
          });
        }
      } else if (event.type === 'chat.status') {
        const state = ['running', 'done', 'error'].includes(String(event.state))
          ? event.state as AgentStep['state']
          : 'running';
        handlers.onStatus({
          id: String(event.statusId || `status-${Date.now()}`),
          kind: 'tool',
          label: String(event.label || '正在处理'),
          state,
        });
      }
    }

    const interaction = task.interaction;
    const nextInteractionId = String(interaction?.interactionId || '');
    if (task.status === 'waiting' && nextInteractionId && nextInteractionId !== interactionId) {
      interactionId = nextInteractionId;
      const supportedMethods = ['select', 'confirm', 'input', 'editor'] as const;
      const methodValue = String(interaction?.method || 'select');
      const method = supportedMethods.includes(methodValue as AgentInteraction['method'])
        ? methodValue as AgentInteraction['method']
        : 'select';
      handlers.onInteraction(
        {
          id: nextInteractionId,
          method,
          title: String(interaction?.title || '需要你的确认'),
          message: String(interaction?.message || ''),
          options: Array.isArray(interaction?.options)
            ? interaction.options.map(String).slice(0, 8)
            : [],
          placeholder: String(interaction?.placeholder || ''),
          pending: true,
        },
        async (response) => {
          await request<AgentTaskSnapshot>(
            `/v1/chat-tasks/${encodeURIComponent(taskId)}/interaction`,
            {
              method: 'POST',
              body: JSON.stringify({ interactionId: nextInteractionId, response }),
            },
            token,
          );
        },
      );
    }

    if (task.status === 'completed') {
      const message = task.message;
      if (!message?.id || (!message.text && !message.attachments?.length)) {
        throw new Error('Agent 服务返回了无效消息');
      }
      return message;
    }
    if (task.status === 'failed') throw new Error(task.error || 'Agent 执行失败');
    if (task.status === 'cancelled') throw new AgentCancelledError();
    await delay(1_000);
  }
}
