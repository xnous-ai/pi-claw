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
};

export type ChatAttachmentUpload = ChatAttachment & {
  data: string;
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
  onInteraction: (
    interaction: AgentInteraction,
    respond: (response: InteractionResponse) => void,
  ) => void;
  onStatus: (step: AgentStep) => void;
};

export type Conversation = {
  id: string;
  title: string;
  deviceId: string;
  updatedAt: string;
  messages: AgentMessage[];
};

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
  kind: 'skill' | 'extension';
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
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<AgentMessage> {
  if (isDemoMode) {
    handlers.onStatus({ id: 'demo', label: '正在整理回复', state: 'running' });
    await delay(350);
    handlers.onDelta('已收到你的消息。');
    handlers.onStatus({ id: 'demo', label: '回复已生成', state: 'done' });
    return {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      text: '已收到你的消息。',
      createdAt: new Date().toISOString(),
    };
  }

  const websocketUrl = `${API_URL!.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:')}/v1/chat/ws`;
  return new Promise<AgentMessage>((resolve, reject) => {
    const socket = new WebSocket(websocketUrl);
    const clientMessageId = `client-${Date.now()}`;
    let accumulated = '';
    let settled = false;
    let heartbeatTimer: ReturnType<typeof setInterval> | undefined;
    const connectionTimer = setTimeout(() => {
      finishWithError('连接聊天服务超时');
    }, 15_000);

    function close() {
      clearTimeout(connectionTimer);
      if (heartbeatTimer) clearInterval(heartbeatTimer);
      signal?.removeEventListener('abort', abort);
      if (socket.readyState === 0 || socket.readyState === 1) socket.close();
    }

    function abort() {
      if (settled) return;
      if (socket.readyState === 1) {
        socket.send(JSON.stringify({ type: 'chat.cancel' }));
      }
      finishCancelled();
    }

    function finishCancelled() {
      if (settled) return;
      settled = true;
      close();
      reject(new AgentCancelledError());
    }

    function finishWithError(message: string) {
      if (settled) return;
      settled = true;
      close();
      reject(new Error(message));
    }

    socket.onopen = () => {
      socket.send(JSON.stringify({ type: 'auth', token }));
    };
    socket.onerror = () => finishWithError('无法连接聊天服务');
    socket.onclose = () => {
      if (!settled) finishWithError('聊天连接已断开');
    };
    socket.onmessage = (event) => {
      let payload: Record<string, any>;
      try {
        payload = JSON.parse(String(event.data));
      } catch {
        finishWithError('聊天服务返回了无效数据');
        return;
      }
      if (payload.type === 'chat.ready') {
        clearTimeout(connectionTimer);
        heartbeatTimer = setInterval(() => {
          if (socket.readyState === 1) socket.send(JSON.stringify({ type: 'heartbeat' }));
        }, 20_000);
        socket.send(JSON.stringify({
          type: 'chat.start',
          clientMessageId,
          deviceId,
          conversationId,
          text,
          attachments,
        }));
      } else if (payload.type === 'chat.delta') {
        const delta = String(payload.delta || '');
        accumulated += delta;
        if (delta) handlers.onDelta(delta);
      } else if (payload.type === 'chat.progress') {
        const progress = String(payload.text || '').trim();
        if (progress) {
          handlers.onStatus({
            id: String(payload.progressId || `progress-${Date.now()}`),
            kind: 'text',
            label: progress,
            state: 'done',
          });
        }
      } else if (payload.type === 'chat.status') {
        const state = ['running', 'done', 'error'].includes(payload.state)
          ? payload.state as AgentStep['state']
          : 'running';
        handlers.onStatus({
          id: String(payload.statusId || `status-${Date.now()}`),
          kind: 'tool',
          label: String(payload.label || '正在处理'),
          state,
        });
      } else if (payload.type === 'chat.interaction') {
        const supportedMethods = ['select', 'confirm', 'input', 'editor'] as const;
        const method = supportedMethods.includes(payload.method)
          ? payload.method as AgentInteraction['method']
          : 'select';
        handlers.onInteraction(
          {
            id: String(payload.interactionId || ''),
            method,
            title: String(payload.title || '需要你的确认'),
            message: String(payload.message || ''),
            options: Array.isArray(payload.options)
              ? payload.options.map(String).slice(0, 8)
              : [],
            placeholder: String(payload.placeholder || ''),
            pending: true,
          },
          (response) => {
            if (socket.readyState !== 1 || !payload.requestId) return;
            socket.send(JSON.stringify({
              type: 'chat.interaction.response',
              requestId: payload.requestId,
              interactionId: payload.interactionId,
              response,
            }));
          },
        );
      } else if (payload.type === 'chat.complete') {
        const message = payload.message || {};
        const finalText = String(message.text || accumulated);
        if (!message.id || !finalText) {
          finishWithError('Agent 服务返回了无效消息');
          return;
        }
        settled = true;
        close();
        resolve({
          id: String(message.id),
          role: 'assistant',
          text: finalText,
          createdAt: String(message.createdAt || new Date().toISOString()),
        });
      } else if (payload.type === 'chat.cancelled') {
        finishCancelled();
      } else if (payload.type === 'chat.error' || payload.type === 'auth.error') {
        finishWithError(String(payload.message || 'Agent 执行失败'));
      }
    };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
}
