export type User = {
  id: string;
  name: string;
  email: string;
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
};

export type Conversation = {
  id: string;
  title: string;
  deviceId: string;
  updatedAt: string;
  messages: AgentMessage[];
};

export type AgentProvider = 'openai' | 'anthropic' | 'google' | 'openrouter';

export type AgentConfiguration = {
  configured: boolean;
  provider: AgentProvider | '';
  model: string;
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

async function request<T>(path: string, init: RequestInit, token?: string): Promise<T> {
  return requestUrl<T>(`${API_URL}${path}`, init, token);
}

function normalizeSession(value: SessionPayload): AuthSession {
  if (!value?.token || !value.user?.id || !value.user.email) {
    throw new Error('登录服务返回了无效数据');
  }
  return {
    token: value.token,
    user: value.user,
    devices: Array.isArray(value.devices) ? value.devices : value.device ? [value.device] : [],
  };
}

export async function login(email: string, password: string): Promise<AuthSession> {
  if (isDemoMode) {
    await delay(450);
    return {
      token: 'demo-token',
      user: { id: 'demo-user', name: '主机用户', email },
      devices: [],
    };
  }
  const session = await request<SessionPayload>('/v1/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
  return normalizeSession(session);
}

export async function register(name: string, email: string, password: string): Promise<AuthSession> {
  if (isDemoMode) {
    await delay(500);
    return {
      token: 'demo-token',
      user: { id: 'demo-user', name, email },
      devices: [],
    };
  }
  const session = await request<SessionPayload>('/v1/auth/register', {
    method: 'POST',
    body: JSON.stringify({ name, email, password }),
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
    return { configured: false, provider: '', model: '' };
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

export async function sendAgentMessage(
  token: string,
  deviceId: string,
  conversationId: string,
  text: string,
): Promise<AgentMessage> {
  if (isDemoMode) {
    await delay(700);
    return {
      id: `assistant-${Date.now()}`,
      role: 'assistant',
      text: `已收到：“${text}”\n\n当前为演示模式。配置真实服务地址后，这里会显示 Pi agent 的回复。`,
      createdAt: new Date().toISOString(),
    };
  }
  const message = await request<AgentMessage>(
    `/v1/devices/${encodeURIComponent(deviceId)}/messages`,
    { method: 'POST', body: JSON.stringify({ conversationId, text }) },
    token,
  );
  if (!message?.id || message.role !== 'assistant' || !message.text) {
    throw new Error('Agent 服务返回了无效消息');
  }
  return message;
}
