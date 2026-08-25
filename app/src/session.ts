import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';

import type { AgentMessage, AuthSession, Conversation, Device } from './api';

const TOKEN_KEY = 'clawpi.auth.token';
const SESSION_KEY = 'clawpi.auth.session';
const MESSAGES_KEY = 'clawpi.chat.messages';
const CONVERSATIONS_KEY = 'clawpi.chat.conversations';

type StoredSession = Omit<AuthSession, 'token' | 'devices' | 'user'> & {
  user: AuthSession['user'] & { email?: string; phone?: string };
  devices?: Device[];
  device?: Device | null;
};

export async function saveSession(session: AuthSession) {
  const { token, ...metadata } = session;
  await Promise.all([
    SecureStore.setItemAsync(TOKEN_KEY, token),
    AsyncStorage.setItem(SESSION_KEY, JSON.stringify(metadata)),
  ]);
}

export async function loadSession(): Promise<AuthSession | null> {
  const [token, metadata] = await Promise.all([
    SecureStore.getItemAsync(TOKEN_KEY),
    AsyncStorage.getItem(SESSION_KEY),
  ]);
  if (!token || !metadata) return null;

  try {
    const stored = JSON.parse(metadata) as StoredSession;
    if (!stored.user?.id) return null;
    const devices = Array.isArray(stored.devices)
      ? stored.devices
      : stored.device
        ? [stored.device]
        : [];
    const legacyPhone = stored.user.email?.split('@', 1)[0] ?? '';
    const phone = stored.user.phone ?? (/^1[3-9]\d{9}$/.test(legacyPhone) ? legacyPhone : '');
    return {
      token,
      user: { id: stored.user.id, name: stored.user.name, phone },
      devices,
    };
  } catch {
    await clearSession();
    return null;
  }
}

export async function saveConversations(conversations: Conversation[]) {
  const trimmed = conversations.slice(0, 50).map((conversation) => ({
    ...conversation,
    messages: conversation.messages.slice(-100),
  }));
  await AsyncStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(trimmed));
}

export async function loadConversations(fallbackDeviceId?: string): Promise<Conversation[]> {
  const [value, legacyMessages] = await Promise.all([
    AsyncStorage.getItem(CONVERSATIONS_KEY),
    AsyncStorage.getItem(MESSAGES_KEY),
  ]);
  if (value) {
    try {
      const conversations = JSON.parse(value) as Conversation[];
      if (Array.isArray(conversations)) {
        return conversations.filter(
          (conversation) => conversation?.id && conversation.deviceId && conversation.updatedAt,
        );
      }
    } catch {
      return [];
    }
  }

  if (!legacyMessages || !fallbackDeviceId) return [];
  try {
    const messages = JSON.parse(legacyMessages) as AgentMessage[];
    if (!Array.isArray(messages) || !messages.length) return [];
    const migrated: Conversation = {
      id: `migrated-${Date.now()}`,
      title: '原有会话',
      deviceId: fallbackDeviceId,
      updatedAt: messages.at(-1)?.createdAt ?? new Date().toISOString(),
      messages: messages.filter((message) => message?.id && message?.text),
    };
    await saveConversations([migrated]);
    await AsyncStorage.removeItem(MESSAGES_KEY);
    return [migrated];
  } catch {
    return [];
  }
}

export async function clearConversations() {
  await AsyncStorage.multiRemove([CONVERSATIONS_KEY, MESSAGES_KEY]);
}

export async function clearSession() {
  await Promise.all([
    SecureStore.deleteItemAsync(TOKEN_KEY),
    AsyncStorage.multiRemove([SESSION_KEY, CONVERSATIONS_KEY, MESSAGES_KEY]),
  ]);
}
