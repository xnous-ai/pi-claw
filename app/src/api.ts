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

export type WifiNetwork = {
  ssid: string;
  signal: number;
  secured: boolean;
};

type SessionPayload = Omit<AuthSession, 'devices'> & {
  devices?: Device[];
  device?: Device | null;
};

const API_URL = process.env.EXPO_PUBLIC_API_URL?.replace(/\/$/, '');
const HOST_SETUP_URL =
  process.env.EXPO_PUBLIC_HOST_SETUP_URL?.replace(/\/$/, '') || 'http://192.168.4.1';
export const isDemoMode = !API_URL;

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
      throw new Error(body?.detail || body?.message || `请求失败（${response.status}）`);
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

export async function scanHostWifiNetworks(): Promise<WifiNetwork[]> {
  if (isDemoMode) {
    await delay(450);
    return [
      { ssid: 'Home WiFi', signal: 88, secured: true },
      { ssid: 'Guest', signal: 61, secured: false },
    ];
  }
  const response = await requestUrl<{ networks?: WifiNetwork[] }>(
    `${HOST_SETUP_URL}/wifi-networks`,
    { method: 'GET' },
    undefined,
    10_000,
  );
  if (!Array.isArray(response.networks)) throw new Error('主机返回了无效的 Wi-Fi 列表');
  return response.networks.filter(
    (network) => network?.ssid && Number.isFinite(network.signal),
  );
}

export async function configureDeviceNetwork(
  token: string,
  name: string,
  wifiName: string,
  wifiPassword: string,
): Promise<Device> {
  if (isDemoMode) {
    await delay(850);
    const suffix = Date.now().toString().slice(-6);
    return {
      id: `demo-host-${suffix}`,
      name,
      serial: `CP-${suffix}`,
      status: 'online',
      version: 'ClawPi OS 1.0.0',
      lastSeenAt: new Date().toISOString(),
    };
  }
  const existingDevices = await request<Device[]>('/v1/devices', { method: 'GET' }, token);
  const existingIds = new Set(existingDevices.map((device) => device.id));
  const provisioning = await request<{ claimToken: string }>(
    '/v1/provisioning/sessions',
    { method: 'POST', body: JSON.stringify({ name }) },
    token,
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

  for (let attempt = 0; attempt < 120; attempt += 1) {
    try {
      const devices = await request<Device[]>('/v1/devices', { method: 'GET' }, token);
      const device = configured.device?.id
        ? devices.find((item) => item.id === configured.device?.id)
        : devices.find((item) => !existingIds.has(item.id));
      if (device?.status === 'online') return device;
    } catch {
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
