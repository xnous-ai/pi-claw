import { SymbolView, type AndroidSymbol } from 'expo-symbols';
import { StatusBar as ExpoStatusBar } from 'expo-status-bar';
import { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  BackHandler,
  KeyboardAvoidingView,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';

import {
  configureDeviceAgent,
  configureDeviceNetwork,
  getDevice,
  getDeviceAgentConfig,
  isDemoMode,
  login,
  prepareDeviceProvisioning,
  register,
  refreshHostWifiNetworks,
  releaseDevice,
  scanHostWifiNetworks,
  sendAgentMessage,
  type AgentProvider,
  type AgentConfiguration,
  type AgentModelOption,
  type AgentProviderOption,
  type AgentMessage,
  type AuthSession,
  type Conversation,
  type Device,
  type DeviceProvisioning,
  type WifiNetwork,
} from './src/api';
import {
  clearSession,
  loadConversations,
  loadSession,
  saveConversations,
  saveSession,
} from './src/session';

type MainTab = 'conversations' | 'devices' | 'profile';
type ProfilePage = 'account' | 'support' | 'terms' | 'privacy' | 'about';
type Route =
  | { name: 'root' }
  | { name: 'chat'; conversationId: string }
  | { name: 'device'; deviceId: string }
  | { name: 'agent-config'; deviceId: string }
  | { name: 'add-device' }
  | { name: 'profile-detail'; page: ProfilePage };

const colors = {
  ink: '#101828',
  muted: '#596579',
  subtle: '#7B8798',
  background: '#F4F6F8',
  surface: '#FFFFFF',
  line: '#DCE1E8',
  accent: '#2563EB',
  accentSoft: '#E9F0FF',
  teal: '#087F72',
  tealSoft: '#E3F5F1',
  success: '#16805D',
  successSoft: '#E6F5EF',
  warning: '#966100',
  warningSoft: '#FFF2D5',
  danger: '#B4233A',
  dangerSoft: '#FCE8EB',
};

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败，请稍后重试';
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date);
  }
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' }).format(date);
}

function formatLastSeen(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未知';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function createWelcomeMessage(): AgentMessage {
  return {
    id: `welcome-${Date.now()}`,
    role: 'assistant',
    text: '你好，我是这台主机上的 Pi agent。告诉我你想完成什么。',
    createdAt: new Date().toISOString(),
  };
}

function AppFrame({ children }: { children: React.ReactNode }) {
  return (
    <SafeAreaView edges={['top', 'bottom']} style={styles.appFrame}>
      <ExpoStatusBar style="dark" />
      {children}
    </SafeAreaView>
  );
}

function Icon({ name, color = colors.ink, size = 24 }: { name: AndroidSymbol; color?: string; size?: number }) {
  return (
    <SymbolView
      fallback={<View style={{ height: size, width: size }} />}
      name={{ android: name }}
      size={size}
      style={{ height: size, width: size }}
      tintColor={color}
    />
  );
}

function IconButton({
  icon,
  label,
  onPress,
  disabled = false,
}: {
  icon: AndroidSymbol;
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityLabel={label}
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [styles.iconButton, disabled && styles.buttonDisabled, pressed && styles.pressed]}
    >
      <Icon color={colors.accent} name={icon} />
    </Pressable>
  );
}

function PrimaryButton({
  label,
  onPress,
  loading = false,
  disabled = false,
  icon,
}: {
  label: string;
  onPress: () => void;
  loading?: boolean;
  disabled?: boolean;
  icon?: AndroidSymbol;
}) {
  const blocked = disabled || loading;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled: blocked, busy: loading }}
      disabled={blocked}
      onPress={onPress}
      style={({ pressed }) => [
        styles.primaryButton,
        blocked && styles.buttonDisabled,
        pressed && !blocked && styles.pressed,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={colors.surface} size="small" />
      ) : (
        <View style={styles.buttonContent}>
          {icon && <Icon color={colors.surface} name={icon} size={20} />}
          <Text style={[styles.primaryButtonText, icon && styles.buttonTextWithIcon]}>{label}</Text>
        </View>
      )}
    </Pressable>
  );
}

function TextButton({ label, onPress, danger = false }: { label: string; onPress: () => void; danger?: boolean }) {
  return (
    <Pressable
      accessibilityRole="button"
      hitSlop={8}
      onPress={onPress}
      style={({ pressed }) => [styles.textButton, pressed && styles.pressed]}
    >
      <Text style={[styles.textButtonLabel, danger && styles.dangerText]}>{label}</Text>
    </Pressable>
  );
}

function Field({
  label,
  value,
  onChangeText,
  placeholder,
  secureTextEntry = false,
  keyboardType = 'default',
}: {
  label: string;
  value: string;
  onChangeText: (value: string) => void;
  placeholder: string;
  secureTextEntry?: boolean;
  keyboardType?: 'default' | 'email-address';
}) {
  return (
    <View style={styles.fieldGroup}>
      <Text style={styles.fieldLabel}>{label}</Text>
      <TextInput
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType={keyboardType}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={colors.subtle}
        secureTextEntry={secureTextEntry}
        style={styles.fieldInput}
        value={value}
      />
    </View>
  );
}

function ProviderPicker({
  provider,
  providers,
  onChange,
}: {
  provider: AgentProvider;
  providers: AgentProviderOption[];
  onChange: (provider: AgentProvider) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState('');
  const selected = providers.find((item) => item.id === provider);
  const normalizedQuery = query.trim().toLowerCase();
  const visible = normalizedQuery
    ? providers.filter((item) => `${item.label} ${item.id}`.toLowerCase().includes(normalizedQuery))
    : providers;

  return (
    <View style={styles.providerPicker}>
      <Text style={styles.fieldLabel}>服务商</Text>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        disabled={!providers.length}
        onPress={() => setExpanded((current) => !current)}
        style={({ pressed }) => [styles.selectControl, !providers.length && styles.buttonDisabled, pressed && styles.rowPressed]}
      >
        <Text numberOfLines={1} style={selected ? styles.selectValue : styles.selectPlaceholder}>
          {selected?.label ?? (providers.length ? '选择服务商' : '正在读取服务商')}
        </Text>
        <Icon color={colors.subtle} name={expanded ? 'expand_less' : 'expand_more'} size={22} />
      </Pressable>
      {expanded && (
        <View accessibilityRole="radiogroup" style={styles.selectMenu}>
          <TextInput
            autoCapitalize="none"
            autoCorrect={false}
            onChangeText={setQuery}
            placeholder="搜索服务商"
            placeholderTextColor={colors.subtle}
            style={styles.selectSearch}
            value={query}
          />
          {visible.map((item, index) => {
            const checked = provider === item.id;
            return (
              <Pressable
                accessibilityRole="radio"
                accessibilityState={{ checked }}
                key={item.id}
                onPress={() => { onChange(item.id); setExpanded(false); setQuery(''); }}
                style={({ pressed }) => [
                  styles.selectRow,
                  index < visible.length - 1 && styles.rowDivider,
                  checked && styles.selectRowSelected,
                  pressed && styles.rowPressed,
                ]}
              >
                <View style={[styles.providerRadio, checked && styles.providerRadioSelected]} />
                <View style={styles.selectRowCopy}>
                  <Text style={[styles.providerLabel, checked && styles.providerLabelSelected]}>{item.label}</Text>
                  <Text style={styles.providerId}>{item.id}</Text>
                </View>
              </Pressable>
            );
          })}
          {!visible.length && <Text style={styles.selectEmpty}>没有匹配的服务商</Text>}
        </View>
      )}
    </View>
  );
}

function ModelPicker({
  model,
  models,
  onChange,
}: {
  model: string;
  models: AgentModelOption[];
  onChange: (model: string) => void;
}) {
  const options = model && !models.some((item) => item.id === model)
    ? [{ id: model, name: model, reasoning: false, contextWindow: 0 }, ...models]
    : models;
  return (
    <View style={styles.modelPicker}>
      <Text style={styles.fieldLabel}>模型</Text>
      <View accessibilityRole="radiogroup" style={styles.modelList}>
        <Pressable
          accessibilityRole="radio"
          accessibilityState={{ checked: !model }}
          onPress={() => onChange('')}
          style={({ pressed }) => [styles.modelRow, styles.rowDivider, !model && styles.selectRowSelected, pressed && styles.rowPressed]}
        >
          <View style={[styles.providerRadio, !model && styles.providerRadioSelected]} />
          <View style={styles.selectRowCopy}>
            <Text style={[styles.providerLabel, !model && styles.providerLabelSelected]}>Pi 默认模型</Text>
            <Text style={styles.providerId}>自动选择</Text>
          </View>
        </Pressable>
        {options.map((item, index) => {
          const checked = model === item.id;
          return (
            <Pressable
              accessibilityRole="radio"
              accessibilityState={{ checked }}
              key={item.id}
              onPress={() => onChange(item.id)}
              style={({ pressed }) => [
                styles.modelRow,
                index < options.length - 1 && styles.rowDivider,
                checked && styles.selectRowSelected,
                pressed && styles.rowPressed,
              ]}
            >
              <View style={[styles.providerRadio, checked && styles.providerRadioSelected]} />
              <View style={styles.selectRowCopy}>
                <Text numberOfLines={2} style={[styles.providerLabel, checked && styles.providerLabelSelected]}>{item.name}</Text>
                <Text style={styles.providerId}>{item.id}{item.reasoning ? ' · 推理模型' : ''}</Text>
              </View>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

function ModeBadge() {
  return (
    <View style={isDemoMode ? styles.demoBadge : styles.liveBadge}>
      <View style={isDemoMode ? styles.demoDot : styles.liveDot} />
      <Text style={isDemoMode ? styles.demoBadgeText : styles.liveBadgeText}>
        {isDemoMode ? '演示模式' : '服务已连接'}
      </Text>
    </View>
  );
}

function PageHeader({
  title,
  subtitle,
  onBack,
  action,
}: {
  title: string;
  subtitle?: string;
  onBack?: () => void;
  action?: React.ReactNode;
}) {
  return (
    <View style={styles.pageHeader}>
      {onBack && <IconButton icon="arrow_back" label="返回" onPress={onBack} />}
      <View style={[styles.pageHeaderCopy, !onBack && styles.pageHeaderCopyRoot]}>
        <Text numberOfLines={1} style={styles.pageTitle}>{title}</Text>
        {!!subtitle && <Text numberOfLines={1} style={styles.pageSubtitle}>{subtitle}</Text>}
      </View>
      {action ?? (onBack ? <View style={styles.iconButtonPlaceholder} /> : null)}
    </View>
  );
}

function BootScreen() {
  return (
    <AppFrame>
      <View style={styles.bootScreen}>
        <View style={styles.brandMark}><Text style={styles.brandMarkText}>P</Text></View>
        <Text style={styles.bootTitle}>ClawPi</Text>
        <ActivityIndicator color={colors.accent} size="small" />
      </View>
    </AppFrame>
  );
}

function AuthScreen({ onAuthenticated }: { onAuthenticated: (session: AuthSession) => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit() {
    const normalizedEmail = email.trim().toLowerCase();
    if (mode === 'register' && !name.trim()) return setError('请输入你的称呼');
    if (!/^\S+@\S+\.\S+$/.test(normalizedEmail)) return setError('请输入有效的邮箱地址');
    if (password.length < 8) return setError('密码至少需要 8 位');

    setError('');
    setLoading(true);
    try {
      const next = mode === 'login'
        ? await login(normalizedEmail, password)
        : await register(name.trim(), normalizedEmail, password);
      await saveSession(next);
      onAuthenticated(next);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  }

  return (
    <AppFrame>
      <KeyboardAvoidingView behavior="height" style={styles.screen}>
        <ScrollView contentContainerStyle={styles.authContent} keyboardShouldPersistTaps="handled">
          <View style={styles.authTopRow}>
            <View style={styles.brandMark}><Text style={styles.brandMarkText}>P</Text></View>
            <ModeBadge />
          </View>
          <Text style={styles.authTitle}>{mode === 'login' ? '欢迎回来' : '创建账号'}</Text>
          <Text style={styles.authSubtitle}>
            {mode === 'login' ? '登录后管理你的 AI 主机与会话。' : '使用购买主机时预留的邮箱创建账号。'}
          </Text>
          <View style={styles.formBlock}>
            {mode === 'register' && <Field label="称呼" onChangeText={setName} placeholder="怎么称呼你" value={name} />}
            <Field keyboardType="email-address" label="邮箱" onChangeText={setEmail} placeholder="name@example.com" value={email} />
            <Field label="密码" onChangeText={setPassword} placeholder="至少 8 位" secureTextEntry value={password} />
            {!!error && <Text style={styles.errorText}>{error}</Text>}
            <PrimaryButton label={mode === 'login' ? '登录' : '创建账号'} loading={loading} onPress={submit} />
          </View>
          <View style={styles.authSwitchRow}>
            <Text style={styles.authSwitchHint}>{mode === 'login' ? '第一次使用？' : '已经有账号？'}</Text>
            <TextButton label={mode === 'login' ? '创建账号' : '返回登录'} onPress={() => {
              setMode((current) => current === 'login' ? 'register' : 'login');
              setError('');
            }} />
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </AppFrame>
  );
}

function AddDeviceScreen({
  session,
  canGoBack,
  onBack,
  onAdded,
  onSignOut,
}: {
  session: AuthSession;
  canGoBack: boolean;
  onBack: () => void;
  onAdded: (device: Device) => Promise<void>;
  onSignOut: () => void;
}) {
  const [step, setStep] = useState<'prepare' | 'network' | 'agent'>('prepare');
  const [hostName, setHostName] = useState(`AI 主机 ${session.devices.length + 1}`);
  const [wifiName, setWifiName] = useState('');
  const [wifiPassword, setWifiPassword] = useState('');
  const [wifiNetworks, setWifiNetworks] = useState<WifiNetwork[]>([]);
  const [scanningWifi, setScanningWifi] = useState(false);
  const [wifiScanError, setWifiScanError] = useState('');
  const [manualWifi, setManualWifi] = useState(false);
  const [provider, setProvider] = useState<AgentProvider>('');
  const [providers, setProviders] = useState<AgentProviderOption[]>([]);
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [pendingDevice, setPendingDevice] = useState<Device | null>(null);
  const [provisioning, setProvisioning] = useState<DeviceProvisioning | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function prepareAndOpenWifiSettings() {
    if (!hostName.trim()) return setError('请输入主机名称');
    setError('');
    setLoading(true);
    try {
      const prepared = await prepareDeviceProvisioning(session.token, hostName.trim());
      setProvisioning(prepared);
      await Linking.sendIntent('android.settings.WIFI_SETTINGS');
    } catch (prepareError) {
      setProvisioning(null);
      setError(errorMessage(prepareError));
    } finally {
      setLoading(false);
    }
  }

  async function scanWifi(refresh = false) {
    setScanningWifi(true);
    setWifiScanError('');
    try {
      const networks = refresh
        ? await refreshHostWifiNetworks()
        : await scanHostWifiNetworks();
      setWifiNetworks(networks);
      setManualWifi(!networks.length);
      if (!networks.length) setWifiScanError('附近没有可用网络，请手动输入 Wi-Fi 名称');
    } catch (scanError) {
      setWifiNetworks([]);
      setManualWifi(true);
      setWifiScanError(errorMessage(scanError));
    } finally {
      setScanningWifi(false);
    }
  }

  function confirmWifiRefresh() {
    Alert.alert(
      '刷新附近 Wi-Fi？',
      '主机热点会短暂断开，恢复后 App 将自动读取新列表。',
      [
        { text: '取消', style: 'cancel' },
        { text: '刷新', onPress: () => void scanWifi(true) },
      ],
    );
  }

  function openNetworkStep() {
    if (!provisioning || Date.parse(provisioning.expiresAt) <= Date.now()) {
      setError('请保持手机联网，先准备绑定并打开 Wi-Fi 设置');
      return;
    }
    setError('');
    setStep('network');
    void scanWifi();
  }

  async function submitNetwork() {
    if (!provisioning) return setError('绑定准备已失效，请返回上一步重新准备');
    if (!wifiName.trim()) return setError('请输入家庭 Wi-Fi 名称');
    if (wifiPassword.length < 8) return setError('Wi-Fi 密码至少需要 8 位');
    setError('');
    setLoading(true);
    try {
      const device = await configureDeviceNetwork(
        session.token,
        provisioning,
        wifiName.trim(),
        wifiPassword,
      );
      const config = await getDeviceAgentConfig(session.token, device.id);
      if (!config.providers.length) throw new Error('主机没有返回可用服务商');
      setPendingDevice(device);
      setProviders(config.providers);
      setProvider(config.provider || config.providers[0].id);
      setModel(config.model);
      setStep('agent');
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  }

  async function submitAgent() {
    if (!pendingDevice) return setError('主机信息已失效，请重新添加');
    if (!provider) return setError('请选择服务商');
    if (apiKey.trim().length < 8) return setError('请输入有效的 API Key');
    setError('');
    setLoading(true);
    try {
      await configureDeviceAgent(
        session.token,
        pendingDevice.id,
        provider,
        apiKey.trim(),
        model,
      );
      setApiKey('');
      await onAdded({
        ...pendingDevice,
        agentProvider: provider,
        agentModel: model.trim() || undefined,
      });
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  }

  const stepNumber = step === 'prepare' ? 1 : step === 'network' ? 2 : 3;

  return (
    <View style={styles.fullPage}>
      <PageHeader
        action={!canGoBack ? <TextButton label="退出" onPress={onSignOut} /> : undefined}
        onBack={canGoBack ? onBack : undefined}
        subtitle={`步骤 ${stepNumber} / 3`}
        title="添加 AI 主机"
      />
      <KeyboardAvoidingView behavior="height" style={styles.screen}>
        <ScrollView contentContainerStyle={styles.pageContent} keyboardShouldPersistTaps="handled">
          <View style={styles.setupIcon}>
            <Icon color={colors.accent} name={step === 'prepare' ? 'router' : step === 'network' ? 'wifi' : 'key'} size={42} />
          </View>
          {step === 'prepare' && (
            <>
              <Text style={styles.setupTitle}>连接主机热点</Text>
              <Text style={styles.setupDescription}>
                保持手机联网并准备绑定，再连接 ClawPi 开头的主机热点。
              </Text>
              <View style={styles.formBlockCompact}>
                <Field
                  label="主机名称"
                  onChangeText={(value) => { setHostName(value); setProvisioning(null); }}
                  placeholder="例如 客厅主机"
                  value={hostName}
                />
              </View>
              <View style={styles.stepList}>
                <View style={styles.stepRow}><Text style={styles.stepNumber}>1</Text><Text style={styles.stepText}>手机连接主机热点</Text></View>
                <View style={styles.stepRow}><Text style={styles.stepNumber}>2</Text><Text style={styles.stepText}>写入家庭 Wi-Fi</Text></View>
                <View style={styles.stepRow}><Text style={styles.stepNumber}>3</Text><Text style={styles.stepText}>设置模型服务并自动绑定账号</Text></View>
              </View>
              {!!error && <Text style={styles.errorText}>{error}</Text>}
              <PrimaryButton
                icon="wifi"
                label="准备绑定并打开 Wi-Fi"
                loading={loading}
                onPress={prepareAndOpenWifiSettings}
              />
              <TextButton label="已连接主机热点，继续" onPress={openNetworkStep} />
            </>
          )}
          {step === 'network' && (
            <>
              <Text style={styles.setupTitle}>设置主机网络</Text>
              <Text style={styles.setupDescription}>写入后主机会关闭热点、连接家庭网络并自动绑定当前账号，请保持手机联网。</Text>
              <View style={styles.formBlock}>
                <View style={styles.wifiSectionHeader}>
                  <Text style={styles.wifiSectionLabel}>选择 Wi-Fi</Text>
                  <IconButton icon="refresh" label="重新扫描" onPress={confirmWifiRefresh} />
                </View>
                {scanningWifi ? (
                  <View style={styles.wifiScanning}>
                    <ActivityIndicator color={colors.accent} size="small" />
                    <Text style={styles.wifiScanningText}>正在读取主机附近的网络</Text>
                  </View>
                ) : wifiNetworks.length && !manualWifi ? (
                  <View accessibilityRole="radiogroup" style={styles.wifiNetworkList}>
                    {wifiNetworks.map((network, index) => {
                      const selected = network.ssid === wifiName;
                      return (
                        <Pressable
                          accessibilityLabel={`${network.ssid}，信号 ${network.signal}%${network.secured ? '' : '，开放网络不支持'}`}
                          accessibilityRole="radio"
                          accessibilityState={{ checked: selected, disabled: !network.secured }}
                          disabled={!network.secured}
                          key={network.ssid}
                          onPress={() => { setWifiName(network.ssid); setWifiPassword(''); setError(''); }}
                          style={({ pressed }) => [
                            styles.wifiNetworkRow,
                            index < wifiNetworks.length - 1 && styles.rowDivider,
                            selected && styles.wifiNetworkRowSelected,
                            !network.secured && styles.wifiNetworkRowDisabled,
                            pressed && styles.rowPressed,
                          ]}
                        >
                          <Icon color={selected ? colors.accent : colors.muted} name="wifi" size={24} />
                          <View style={styles.wifiNetworkCopy}>
                            <Text numberOfLines={1} style={[styles.wifiNetworkName, selected && styles.wifiNetworkNameSelected]}>{network.ssid}</Text>
                            <Text style={styles.wifiNetworkMeta}>{network.secured ? `信号 ${network.signal}%` : '开放网络暂不支持'}</Text>
                          </View>
                          {network.secured && <Icon color={colors.subtle} name="lock" size={18} />}
                          {selected && <Icon color={colors.accent} name="check_circle" size={23} />}
                        </Pressable>
                      );
                    })}
                  </View>
                ) : (
                  <Field label="Wi-Fi 名称" onChangeText={setWifiName} placeholder="手动输入 Wi-Fi 名称" value={wifiName} />
                )}
                {!!wifiName && <Field label={`${wifiName} 密码`} onChangeText={setWifiPassword} placeholder="至少 8 位" secureTextEntry value={wifiPassword} />}
                {!!wifiScanError && <Text style={styles.wifiScanError}>{wifiScanError}</Text>}
                {!scanningWifi && (
                  <TextButton
                    label={manualWifi ? '重新扫描附近 Wi-Fi' : '找不到网络？手动输入'}
                    onPress={() => manualWifi ? confirmWifiRefresh() : setManualWifi(true)}
                  />
                )}
                {!!error && <Text style={styles.errorText}>{error}</Text>}
                <PrimaryButton label="下一步" loading={loading} onPress={submitNetwork} />
                <TextButton label="返回上一步" onPress={() => { setError(''); setStep('prepare'); }} />
              </View>
            </>
          )}
          {step === 'agent' && (
            <>
              <Text style={styles.setupTitle}>设置模型服务</Text>
              <Text style={styles.setupDescription}>主机已联网。选择服务商并填写 API Key，模型名称可留空并使用 Pi agent 默认值。</Text>
              <ProviderPicker onChange={setProvider} provider={provider} providers={providers} />
              <View style={styles.formBlockCompact}>
                <Field label="API Key" onChangeText={setApiKey} placeholder="输入服务商 API Key" secureTextEntry value={apiKey} />
                <Field label="模型（选填）" onChangeText={setModel} placeholder="例如 gpt-5-mini" value={model} />
                <View style={styles.securityNote}>
                  <Icon color={colors.success} name="lock" size={20} />
                  <Text style={styles.securityNoteText}>Key 经服务端实时转发到当前主机，不会写入 ClawPi 数据库。</Text>
                </View>
                {!!error && <Text style={styles.errorText}>{error}</Text>}
                <PrimaryButton label="保存并完成绑定" loading={loading} onPress={submitAgent} />
              </View>
            </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </View>
  );
}

function DeviceFilter({
  devices,
  selectedId,
  onSelect,
}: {
  devices: Device[];
  selectedId: string;
  onSelect: (deviceId: string) => void;
}) {
  return (
    <ScrollView
      contentContainerStyle={styles.filterList}
      horizontal
      showsHorizontalScrollIndicator={false}
      style={styles.filterScroll}
    >
      {devices.map((device) => {
        const selected = device.id === selectedId;
        return (
          <Pressable
            accessibilityRole="button"
            accessibilityState={{ selected }}
            key={device.id}
            onPress={() => onSelect(device.id)}
            style={({ pressed }) => [styles.filterItem, selected && styles.filterItemSelected, pressed && styles.pressed]}
          >
            <View style={device.status === 'online' ? styles.onlineDot : styles.offlineDot} />
            <Text numberOfLines={1} style={[styles.filterLabel, selected && styles.filterLabelSelected]}>{device.name}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

function ConversationListScreen({
  devices,
  conversations,
  onOpen,
  onCreate,
}: {
  devices: Device[];
  conversations: Conversation[];
  onOpen: (conversationId: string) => void;
  onCreate: (deviceId: string) => void;
}) {
  const [selectedDeviceId, setSelectedDeviceId] = useState(devices[0]?.id ?? '');
  const selectedDevice = devices.find((device) => device.id === selectedDeviceId) ?? devices[0];
  const visible = conversations
    .filter((conversation) => conversation.deviceId === selectedDevice.id)
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));

  return (
    <View style={styles.fullPage}>
      <PageHeader
        action={<IconButton icon="add" label="新建会话" onPress={() => onCreate(selectedDevice.id)} />}
        subtitle={`${conversations.length} 个会话`}
        title="会话"
      />
      <DeviceFilter devices={devices} onSelect={setSelectedDeviceId} selectedId={selectedDevice.id} />
      <ScrollView contentContainerStyle={styles.listContent}>
        {visible.length ? (
          <View style={styles.listSurface}>
            {visible.map((conversation, index) => {
              const lastMessage = conversation.messages.at(-1);
              return (
                <Pressable
                  accessibilityRole="button"
                  key={conversation.id}
                  onPress={() => onOpen(conversation.id)}
                  style={({ pressed }) => [
                    styles.conversationRow,
                    index < visible.length - 1 && styles.rowDivider,
                    pressed && styles.rowPressed,
                  ]}
                >
                  <View style={styles.conversationIcon}><Icon color={colors.accent} name="forum" /></View>
                  <View style={styles.conversationCopy}>
                    <View style={styles.conversationTitleRow}>
                      <Text numberOfLines={1} style={styles.rowTitle}>{conversation.title}</Text>
                      <Text style={styles.rowTime}>{formatTime(conversation.updatedAt)}</Text>
                    </View>
                    <Text numberOfLines={2} style={styles.rowPreview}>{lastMessage?.text ?? '暂无消息'}</Text>
                  </View>
                  <Icon color={colors.subtle} name="chevron_right" size={20} />
                </Pressable>
              );
            })}
          </View>
        ) : (
          <View style={styles.emptyState}>
            <View style={styles.emptyIcon}><Icon color={colors.accent} name="chat" size={34} /></View>
            <Text style={styles.emptyTitle}>还没有会话</Text>
            <Text style={styles.emptyText}>从一段新会话开始与 {selectedDevice.name} 上的 Pi agent 对话。</Text>
            <PrimaryButton icon="add" label="新建会话" onPress={() => onCreate(selectedDevice.id)} />
          </View>
        )}
      </ScrollView>
    </View>
  );
}

function ChatScreen({
  conversation,
  device,
  sending,
  onBack,
  onSend,
}: {
  conversation: Conversation;
  device: Device;
  sending: boolean;
  onBack: () => void;
  onSend: (text: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const scrollRef = useRef<ScrollView>(null);

  useEffect(() => {
    const timer = setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 80);
    return () => clearTimeout(timer);
  }, [conversation.messages, sending]);

  async function submit() {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft('');
    setError('');
    try {
      await onSend(text);
    } catch (requestError) {
      setDraft(text);
      setError(errorMessage(requestError));
    }
  }

  return (
    <KeyboardAvoidingView behavior="height" style={styles.fullPage}>
      <PageHeader
        onBack={onBack}
        subtitle={`${device.name} · ${device.status === 'online' ? '在线' : '离线'}`}
        title={conversation.title}
      />
      <ScrollView
        contentContainerStyle={styles.messageList}
        keyboardShouldPersistTaps="handled"
        ref={scrollRef}
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.dateDividerText}>今天</Text>
        {conversation.messages.map((message) => (
          <View key={message.id} style={styles.messageGroup}>
            <View style={[styles.messageRow, message.role === 'user' && styles.userMessageRow]}>
              {message.role === 'assistant' && <View style={styles.agentAvatar}><Text style={styles.agentAvatarText}>P</Text></View>}
              <View style={[styles.messageBubble, message.role === 'user' ? styles.userBubble : styles.agentBubble]}>
                <Text style={message.role === 'user' ? styles.userMessageText : styles.agentMessageText}>{message.text}</Text>
                <Text style={message.role === 'user' ? styles.userTime : styles.agentTime}>{formatTime(message.createdAt)}</Text>
              </View>
            </View>
            {message.role === 'user' && !!message.error && (
              <View style={styles.messageError}>
                <Icon color={colors.danger} name="error" size={15} />
                <Text accessibilityLiveRegion="polite" style={styles.messageErrorText}>{message.error}</Text>
              </View>
            )}
          </View>
        ))}
        {sending && (
          <View style={styles.messageRow}>
            <View style={styles.agentAvatar}><Text style={styles.agentAvatarText}>P</Text></View>
            <View style={[styles.messageBubble, styles.agentBubble, styles.typingBubble]}>
              <ActivityIndicator color={colors.accent} size="small" />
              <Text style={styles.typingText}>Pi agent 正在处理</Text>
            </View>
          </View>
        )}
      </ScrollView>
      {!!error && <Text style={styles.composerError}>{error}，消息已保留。</Text>}
      <View style={styles.composerWrap}>
        <TextInput
          accessibilityLabel="消息输入框"
          editable={!sending && device.status === 'online'}
          multiline
          onChangeText={setDraft}
          placeholder={device.status === 'online' ? '发送消息' : '主机离线'}
          placeholderTextColor={colors.subtle}
          style={styles.composerInput}
          value={draft}
        />
        <Pressable
          accessibilityLabel="发送消息"
          accessibilityRole="button"
          disabled={!draft.trim() || sending || device.status !== 'online'}
          onPress={submit}
          style={({ pressed }) => [
            styles.sendButton,
            (!draft.trim() || sending || device.status !== 'online') && styles.sendButtonDisabled,
            pressed && styles.pressed,
          ]}
        >
          <Icon color={colors.surface} name="send" size={22} />
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

function DeviceListScreen({
  devices,
  onAdd,
  onOpen,
}: {
  devices: Device[];
  onAdd: () => void;
  onOpen: (deviceId: string) => void;
}) {
  const onlineCount = devices.filter((device) => device.status === 'online').length;
  return (
    <View style={styles.fullPage}>
      <PageHeader
        action={<IconButton icon="add" label="添加主机" onPress={onAdd} />}
        subtitle={`已绑定 ${devices.length} 台 · ${onlineCount} 台在线`}
        title="主机"
      />
      <ScrollView contentContainerStyle={styles.listContent}>
        <View style={styles.listSurface}>
          {devices.map((device, index) => (
            <Pressable
              accessibilityRole="button"
              key={device.id}
              onPress={() => onOpen(device.id)}
              style={({ pressed }) => [
                styles.deviceRow,
                index < devices.length - 1 && styles.rowDivider,
                pressed && styles.rowPressed,
              ]}
            >
              <View style={styles.deviceIcon}><Icon color={colors.teal} name="dns" size={26} /></View>
              <View style={styles.deviceRowCopy}>
                <Text numberOfLines={1} style={styles.rowTitle}>{device.name}</Text>
                <View style={styles.statusLine}>
                  <View style={device.status === 'online' ? styles.onlineDot : styles.offlineDot} />
                  <Text style={device.status === 'online' ? styles.onlineText : styles.offlineText}>
                    {device.status === 'online' ? '在线' : '离线'} · {device.serial}
                  </Text>
                </View>
              </View>
              <Icon color={colors.subtle} name="chevron_right" size={20} />
            </Pressable>
          ))}
        </View>
        <Text style={styles.sectionHint}>每台主机独立运行 Pi agent，会话也会按主机分别保存。</Text>
      </ScrollView>
    </View>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.detailRow}>
      <Text style={styles.detailLabel}>{label}</Text>
      <Text numberOfLines={2} style={styles.detailValue}>{value}</Text>
    </View>
  );
}

function DeviceDetailScreen({
  device,
  refreshing,
  onBack,
  onConfigure,
  onRefresh,
  onRelease,
}: {
  device: Device;
  refreshing: boolean;
  onBack: () => void;
  onConfigure: () => void;
  onRefresh: () => void;
  onRelease: () => void;
}) {
  return (
    <View style={styles.fullPage}>
      <PageHeader
        action={<IconButton disabled={refreshing} icon="refresh" label="刷新状态" onPress={onRefresh} />}
        onBack={onBack}
        subtitle={device.status === 'online' ? '在线并可访问' : '当前离线'}
        title={device.name}
      />
      <ScrollView contentContainerStyle={styles.pageContent}>
        <View style={styles.deviceHero}>
          <View style={styles.deviceHeroIcon}><Icon color={colors.teal} name="dns" size={42} /></View>
          <View style={styles.deviceHeroCopy}>
            <Text style={styles.deviceName}>{device.name}</Text>
            <View style={styles.statusLine}>
              <View style={device.status === 'online' ? styles.onlineDot : styles.offlineDot} />
              <Text style={device.status === 'online' ? styles.onlineText : styles.offlineText}>
                {isDemoMode ? '演示设备' : device.status === 'online' ? 'Pi agent 已连接' : '等待主机上线'}
              </Text>
            </View>
          </View>
        </View>
        <View style={styles.detailSurface}>
          <DetailRow label="序列号" value={device.serial} />
          <DetailRow label="系统版本" value={device.version} />
          <DetailRow label="最近在线" value={formatLastSeen(device.lastSeenAt)} />
        </View>
        <Text style={styles.listSectionTitle}>Agent</Text>
        <View style={styles.listSurface}>
          <SettingsRow
            icon="key"
            label="模型服务"
            onPress={onConfigure}
            value={device.agentProvider ?? '设置'}
          />
        </View>
        <Pressable accessibilityRole="button" onPress={onRelease} style={({ pressed }) => [styles.dangerAction, pressed && styles.pressed]}>
          <Icon color={colors.danger} name="delete" size={21} />
          <Text style={styles.dangerActionText}>解绑这台主机</Text>
        </Pressable>
      </ScrollView>
    </View>
  );
}

function AgentConfigScreen({
  device,
  onBack,
  onLoad,
  onSave,
}: {
  device: Device;
  onBack: () => void;
  onLoad: () => Promise<AgentConfiguration>;
  onSave: (provider: AgentProvider, apiKey: string | undefined, model: string) => Promise<AgentConfiguration>;
}) {
  const [provider, setProvider] = useState<AgentProvider>(device.agentProvider ?? 'openai');
  const [configuredProvider, setConfiguredProvider] = useState<AgentProvider | null>(device.agentProvider ?? null);
  const [providers, setProviders] = useState<AgentProviderOption[]>([]);
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(device.agentModel ?? '');
  const [models, setModels] = useState<AgentModelOption[]>([]);
  const [loadingConfig, setLoadingConfig] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const requiresApiKey = !configuredProvider || provider !== configuredProvider;

  useEffect(() => {
    let active = true;
    onLoad()
      .then((config) => {
        if (!active) return;
        setProviders(config.providers);
        setModels(config.models);
        if (config.configured && config.provider) {
          setProvider(config.provider);
          setConfiguredProvider(config.provider);
          setModel(config.model);
        } else if (config.providers.length) {
          setProvider(config.providers[0].id);
        }
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError));
      })
      .finally(() => {
        if (active) setLoadingConfig(false);
      });
    return () => { active = false; };
  }, [device.id]);

  async function submit() {
    const nextKey = apiKey.trim();
    if (requiresApiKey && nextKey.length < 8) return setError('切换服务商时请输入新的 API Key');
    if (nextKey && nextKey.length < 8) return setError('请输入有效的 API Key');
    setError('');
    setSaved(false);
    setLoading(true);
    try {
      const config = await onSave(provider, nextKey || undefined, model.trim());
      setApiKey('');
      setConfiguredProvider(provider);
      setProviders(config.providers);
      setModels(config.models);
      setModel(config.model);
      setSaved(true);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  }

  return (
    <View style={styles.fullPage}>
      <PageHeader onBack={onBack} subtitle={device.name} title="模型服务" />
      <KeyboardAvoidingView behavior="height" style={styles.screen}>
        <ScrollView contentContainerStyle={styles.pageContent} keyboardShouldPersistTaps="handled">
          <Text style={styles.setupTitle}>选择服务商</Text>
          <Text style={styles.setupDescription}>设置会实时发送到这台主机，并用于之后的新聊天请求。</Text>
          {loadingConfig && (
            <View style={styles.configLoading}>
              <ActivityIndicator color={colors.accent} size="small" />
              <Text style={styles.configLoadingText}>正在读取主机当前配置</Text>
            </View>
          )}
          <ProviderPicker
            onChange={(value) => {
              setProvider(value);
              setModel('');
              if (value !== configuredProvider) setModels([]);
              setSaved(false);
              setError('');
            }}
            provider={provider}
            providers={providers}
          />
          <View style={styles.formBlockCompact}>
            <Field
              label={requiresApiKey ? 'API Key' : '新 API Key（选填）'}
              onChangeText={(value) => { setApiKey(value); setSaved(false); }}
              placeholder={requiresApiKey ? '输入所选服务商的 API Key' : '留空则继续使用当前 Key'}
              secureTextEntry
              value={apiKey}
            />
            {!!models.length && <ModelPicker model={model} models={models} onChange={(value) => { setModel(value); setSaved(false); }} />}
            {!models.length && (
              <Text style={styles.modelHint}>
                {requiresApiKey ? '保存新服务商和 API Key 后，将读取 Pi 提供的可用模型。' : 'Pi 暂未返回该服务商的可用模型，将使用默认模型。'}
              </Text>
            )}
            <View style={styles.securityNote}>
              <Icon color={colors.success} name="lock" size={20} />
              <Text style={styles.securityNoteText}>Key 仅经服务端实时转发并保存在当前主机，不会写入云端数据库。</Text>
            </View>
            {!!saved && <Text style={styles.successText}>设置已保存并下发到主机。</Text>}
            {!!error && <Text style={styles.errorText}>{error}</Text>}
            <PrimaryButton icon="save" label="保存设置" loading={loading} onPress={submit} />
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </View>
  );
}

function SettingsRow({
  icon,
  label,
  value,
  onPress,
}: {
  icon: AndroidSymbol;
  label: string;
  value?: string;
  onPress: () => void;
}) {
  return (
    <Pressable accessibilityRole="button" onPress={onPress} style={({ pressed }) => [styles.settingsRow, pressed && styles.rowPressed]}>
      <Icon color={colors.muted} name={icon} size={23} />
      <Text style={styles.settingsLabel}>{label}</Text>
      {!!value && <Text numberOfLines={1} style={styles.settingsValue}>{value}</Text>}
      <Icon color={colors.subtle} name="chevron_right" size={20} />
    </Pressable>
  );
}

function ProfileScreen({ session, onOpen, onSignOut }: { session: AuthSession; onOpen: (page: ProfilePage) => void; onSignOut: () => void }) {
  return (
    <View style={styles.fullPage}>
      <PageHeader subtitle="账号与服务" title="我的" />
      <ScrollView contentContainerStyle={styles.listContent}>
        <View style={styles.profileIdentity}>
          <View style={styles.profileAvatar}><Text style={styles.profileAvatarText}>{session.user.name.slice(0, 1).toUpperCase()}</Text></View>
          <View style={styles.profileCopy}>
            <Text style={styles.profileName}>{session.user.name}</Text>
            <Text style={styles.profileEmail}>{session.user.email}</Text>
          </View>
          <ModeBadge />
        </View>

        <Text style={styles.listSectionTitle}>账号</Text>
        <View style={styles.listSurface}>
          <SettingsRow icon="manage_accounts" label="账号管理" onPress={() => onOpen('account')} />
        </View>

        <Text style={styles.listSectionTitle}>服务与支持</Text>
        <View style={styles.listSurface}>
          <SettingsRow icon="support_agent" label="联系客服" onPress={() => onOpen('support')} />
          <View style={styles.settingsDivider} />
          <SettingsRow icon="description" label="用户协议" onPress={() => onOpen('terms')} />
          <View style={styles.settingsDivider} />
          <SettingsRow icon="privacy_tip" label="隐私政策" onPress={() => onOpen('privacy')} />
          <View style={styles.settingsDivider} />
          <SettingsRow icon="info" label="关于 ClawPi" onPress={() => onOpen('about')} value="1.0.0" />
        </View>

        <Pressable accessibilityRole="button" onPress={onSignOut} style={({ pressed }) => [styles.signOutButton, pressed && styles.pressed]}>
          <Icon color={colors.danger} name="logout" size={21} />
          <Text style={styles.signOutText}>退出登录</Text>
        </Pressable>
      </ScrollView>
    </View>
  );
}

function ProfileDetailScreen({ page, session, onBack }: { page: ProfilePage; session: AuthSession; onBack: () => void }) {
  const supportEmail = process.env.EXPO_PUBLIC_SUPPORT_EMAIL || '尚未配置';
  const titles: Record<ProfilePage, string> = {
    account: '账号管理',
    support: '联系客服',
    terms: '用户协议',
    privacy: '隐私政策',
    about: '关于 ClawPi',
  };

  return (
    <View style={styles.fullPage}>
      <PageHeader onBack={onBack} title={titles[page]} />
      <ScrollView contentContainerStyle={styles.pageContent}>
        {page === 'account' && (
          <View style={styles.detailSurface}>
            <DetailRow label="称呼" value={session.user.name} />
            <DetailRow label="登录邮箱" value={session.user.email} />
            <DetailRow label="账号 ID" value={session.user.id} />
            <DetailRow label="已绑定主机" value={`${session.devices.length} 台`} />
          </View>
        )}
        {page === 'support' && (
          <>
            <View style={styles.supportIcon}><Icon color={colors.accent} name="support_agent" size={40} /></View>
            <Text style={styles.detailTitle}>ClawPi 客服</Text>
            <Text style={styles.detailBody}>工作日 9:00 - 18:00。提交问题时请附上主机序列号，便于快速定位。</Text>
            <View style={styles.detailSurface}>
              <DetailRow label="服务邮箱" value={supportEmail} />
              <DetailRow label="设备数量" value={`${session.devices.length} 台`} />
            </View>
          </>
        )}
        {page === 'terms' && (
          <>
            <Text style={styles.detailTitle}>ClawPi 用户协议</Text>
            <Text style={styles.detailBody}>生效日期：正式发布前确定</Text>
            <Text style={styles.articleText}>本协议用于说明 ClawPi 账号、AI 主机及 Pi agent 服务的使用规则。用户应妥善保管账号信息，并仅将主机用于合法用途。</Text>
            <Text style={styles.articleText}>正式发布前，需要由法务补充服务范围、内容责任、知识产权、终止服务和争议解决等完整条款。</Text>
          </>
        )}
        {page === 'privacy' && (
          <>
            <Text style={styles.detailTitle}>ClawPi 隐私政策</Text>
            <Text style={styles.detailBody}>生效日期：正式发布前确定</Text>
            <Text style={styles.articleText}>为提供账号登录、主机绑定和远程会话服务，系统会处理账号标识、主机状态以及必要的会话数据。</Text>
            <Text style={styles.articleText}>正式发布前，需要明确数据保存位置、保留周期、第三方处理方以及用户查询和删除数据的方式。</Text>
          </>
        )}
        {page === 'about' && (
          <>
            <View style={styles.aboutMark}><Text style={styles.brandMarkText}>P</Text></View>
            <Text style={styles.detailTitle}>ClawPi</Text>
            <Text style={styles.detailBody}>Android 版本 1.0.0</Text>
            <View style={styles.detailSurface}>
              <DetailRow label="运行模式" value={isDemoMode ? '演示模式' : '正式服务'} />
              <DetailRow label="客户端" value="Android" />
            </View>
          </>
        )}
      </ScrollView>
    </View>
  );
}

function BottomNav({ tab, onChange }: { tab: MainTab; onChange: (tab: MainTab) => void }) {
  const items: { tab: MainTab; label: string; icon: AndroidSymbol }[] = [
    { tab: 'conversations', label: '会话', icon: 'forum' },
    { tab: 'devices', label: '主机', icon: 'dns' },
    { tab: 'profile', label: '我的', icon: 'person' },
  ];
  return (
    <View accessibilityRole="tablist" style={styles.bottomNav}>
      {items.map((item) => {
        const selected = tab === item.tab;
        return (
          <Pressable
            accessibilityRole="tab"
            accessibilityState={{ selected }}
            key={item.tab}
            onPress={() => onChange(item.tab)}
            style={({ pressed }) => [styles.navItem, pressed && styles.pressed]}
          >
            <Icon color={selected ? colors.accent : colors.subtle} name={item.icon} size={24} />
            <Text style={[styles.navLabel, selected && styles.navLabelSelected]}>{item.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

function MainScreen({
  session,
  conversations,
  sending,
  onAddDevice,
  onCreateConversation,
  onConfigureDevice,
  onLoadDeviceConfig,
  onRefreshDevice,
  onReleaseDevice,
  onSend,
  onSignOut,
}: {
  session: AuthSession;
  conversations: Conversation[];
  sending: boolean;
  onAddDevice: (device: Device) => Promise<void>;
  onCreateConversation: (deviceId: string) => Promise<Conversation>;
  onConfigureDevice: (deviceId: string, provider: AgentProvider, apiKey: string | undefined, model: string) => Promise<AgentConfiguration>;
  onLoadDeviceConfig: (deviceId: string) => Promise<AgentConfiguration>;
  onRefreshDevice: (deviceId: string) => Promise<void>;
  onReleaseDevice: (deviceId: string) => Promise<void>;
  onSend: (conversationId: string, text: string) => Promise<void>;
  onSignOut: () => void;
}) {
  const [tab, setTab] = useState<MainTab>('conversations');
  const [route, setRoute] = useState<Route>({ name: 'root' });
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (route.name === 'root') return;
    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      setRoute({ name: 'root' });
      return true;
    });
    return () => subscription.remove();
  }, [route.name]);

  async function createConversation(deviceId: string) {
    const conversation = await onCreateConversation(deviceId);
    setRoute({ name: 'chat', conversationId: conversation.id });
  }

  function confirmRelease(device: Device) {
    Alert.alert('解绑这台主机？', `解绑 ${device.name} 后，该主机的本地会话记录也会移除。`, [
      { text: '取消', style: 'cancel' },
      {
        text: '确认解绑',
        style: 'destructive',
        onPress: async () => {
          try {
            await onReleaseDevice(device.id);
            setRoute({ name: 'root' });
          } catch (releaseError) {
            Alert.alert('无法解绑主机', errorMessage(releaseError));
          }
        },
      },
    ]);
  }

  function confirmSignOut() {
    Alert.alert('退出登录？', '本机保存的会话记录将被清除。', [
      { text: '取消', style: 'cancel' },
      { text: '退出', style: 'destructive', onPress: onSignOut },
    ]);
  }

  let content: React.ReactNode;
  if (route.name === 'chat') {
    const conversation = conversations.find((item) => item.id === route.conversationId);
    const device = conversation && session.devices.find((item) => item.id === conversation.deviceId);
    content = conversation && device ? (
      <ChatScreen
        conversation={conversation}
        device={device}
        onBack={() => setRoute({ name: 'root' })}
        onSend={(text) => onSend(conversation.id, text)}
        sending={sending}
      />
    ) : null;
  } else if (route.name === 'device') {
    const device = session.devices.find((item) => item.id === route.deviceId);
    content = device ? (
      <DeviceDetailScreen
        device={device}
        onBack={() => setRoute({ name: 'root' })}
        onConfigure={() => setRoute({ name: 'agent-config', deviceId: device.id })}
        onRefresh={async () => {
          setRefreshing(true);
          try {
            await onRefreshDevice(device.id);
          } catch (refreshError) {
            Alert.alert('无法刷新主机', errorMessage(refreshError));
          } finally {
            setRefreshing(false);
          }
        }}
        onRelease={() => confirmRelease(device)}
        refreshing={refreshing}
      />
    ) : null;
  } else if (route.name === 'agent-config') {
    const device = session.devices.find((item) => item.id === route.deviceId);
    content = device ? (
      <AgentConfigScreen
        device={device}
        onBack={() => setRoute({ name: 'device', deviceId: device.id })}
        onLoad={() => onLoadDeviceConfig(device.id)}
        onSave={(provider, apiKey, model) => onConfigureDevice(device.id, provider, apiKey, model)}
      />
    ) : null;
  } else if (route.name === 'add-device') {
    content = (
      <AddDeviceScreen
        canGoBack
        onAdded={async (device) => {
          await onAddDevice(device);
          setRoute({ name: 'device', deviceId: device.id });
        }}
        onBack={() => setRoute({ name: 'root' })}
        onSignOut={onSignOut}
        session={session}
      />
    );
  } else if (route.name === 'profile-detail') {
    content = <ProfileDetailScreen onBack={() => setRoute({ name: 'root' })} page={route.page} session={session} />;
  } else {
    content = tab === 'conversations' ? (
      <ConversationListScreen
        conversations={conversations}
        devices={session.devices}
        onCreate={createConversation}
        onOpen={(conversationId) => setRoute({ name: 'chat', conversationId })}
      />
    ) : tab === 'devices' ? (
      <DeviceListScreen
        devices={session.devices}
        onAdd={() => setRoute({ name: 'add-device' })}
        onOpen={(deviceId) => setRoute({ name: 'device', deviceId })}
      />
    ) : (
      <ProfileScreen onOpen={(page) => setRoute({ name: 'profile-detail', page })} onSignOut={confirmSignOut} session={session} />
    );
  }

  return (
    <AppFrame>
      <View style={styles.mainShell}>
        {content}
        {route.name === 'root' && <BottomNav onChange={setTab} tab={tab} />}
      </View>
    </AppFrame>
  );
}

function AppContent() {
  const [booting, setBooting] = useState(true);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    async function restore() {
      try {
        let storedSession = await loadSession();
        if (!isDemoMode && storedSession?.token === 'demo-token') {
          await clearSession();
          storedSession = null;
        }
        const storedConversations = await loadConversations(storedSession?.devices[0]?.id);
        setSession(storedSession);
        setConversations(storedConversations);
      } catch {
        await clearSession();
      } finally {
        setBooting(false);
      }
    }
    restore();
  }, []);

  async function updateSession(next: AuthSession) {
    setSession(next);
    await saveSession(next);
  }

  async function signOut() {
    await clearSession();
    setSession(null);
    setConversations([]);
  }

  async function addDevice(device: Device) {
    if (!session) return;
    await updateSession({ ...session, devices: [...session.devices, device] });
  }

  async function refreshDevice(deviceId: string) {
    if (!session) return;
    const refreshed = await getDevice(session.token, deviceId);
    const current = session.devices.find((device) => device.id === deviceId);
    const merged = current ? { ...current, ...refreshed, name: current.name } : refreshed;
    await updateSession({
      ...session,
      devices: session.devices.map((device) => device.id === deviceId ? merged : device),
    });
  }

  async function configureAgent(
    deviceId: string,
    provider: AgentProvider,
    apiKey: string | undefined,
    model: string,
  ): Promise<AgentConfiguration> {
    if (!session) throw new Error('登录状态已失效');
    await configureDeviceAgent(session.token, deviceId, provider, apiKey, model);
    const config = await getDeviceAgentConfig(session.token, deviceId);
    await updateSession({
      ...session,
      devices: session.devices.map((device) => device.id === deviceId ? {
        ...device,
        agentProvider: config.provider || provider,
        agentModel: config.model || undefined,
      } : device),
    });
    return config;
  }

  async function loadAgentConfig(deviceId: string) {
    if (!session) throw new Error('登录状态已失效');
    const config = await getDeviceAgentConfig(session.token, deviceId);
    if (config.configured && config.provider) {
      await updateSession({
        ...session,
        devices: session.devices.map((device) => device.id === deviceId ? {
          ...device,
          agentProvider: config.provider || undefined,
          agentModel: config.model || undefined,
        } : device),
      });
    }
    return config;
  }

  async function unbindDevice(deviceId: string) {
    if (!session) return;
    await releaseDevice(session.token, deviceId);
    const nextConversations = conversations.filter((item) => item.deviceId !== deviceId);
    await Promise.all([
      updateSession({ ...session, devices: session.devices.filter((device) => device.id !== deviceId) }),
      saveConversations(nextConversations),
    ]);
    setConversations(nextConversations);
  }

  async function createConversation(deviceId: string) {
    const now = new Date().toISOString();
    const conversation: Conversation = {
      id: `conversation-${Date.now()}`,
      title: '新会话',
      deviceId,
      updatedAt: now,
      messages: [createWelcomeMessage()],
    };
    const next = [conversation, ...conversations];
    setConversations(next);
    await saveConversations(next);
    return conversation;
  }

  async function sendMessage(conversationId: string, text: string) {
    if (!session || sending) return;
    const original = conversations.find((item) => item.id === conversationId);
    if (!original) return;
    const userMessage: AgentMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      text,
      createdAt: new Date().toISOString(),
    };
    const title = original.title === '新会话' ? text.slice(0, 22) : original.title;
    const pending: Conversation = {
      ...original,
      title,
      updatedAt: userMessage.createdAt,
      messages: [...original.messages, userMessage],
    };
    setConversations((current) => current.map((item) => item.id === conversationId ? pending : item));
    setSending(true);
    try {
      let reply: AgentMessage;
      try {
        reply = await sendAgentMessage(session.token, original.deviceId, conversationId, text);
      } catch (sendError) {
        const failedMessage = { ...userMessage, error: errorMessage(sendError) };
        const failed = { ...pending, messages: [...original.messages, failedMessage] };
        const next = conversations
          .map((item) => item.id === conversationId ? failed : item)
          .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
        setConversations(next);
        await saveConversations(next);
        return;
      }
      const completed = { ...pending, updatedAt: reply.createdAt, messages: [...pending.messages, reply] };
      const next = conversations
        .map((item) => item.id === conversationId ? completed : item)
        .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
      setConversations(next);
      await saveConversations(next);
    } finally {
      setSending(false);
    }
  }

  if (booting) return <BootScreen />;
  if (!session) return <AuthScreen onAuthenticated={setSession} />;
  if (!session.devices.length) {
    return (
      <AppFrame>
        <AddDeviceScreen
          canGoBack={false}
          onAdded={addDevice}
          onBack={() => undefined}
          onSignOut={signOut}
          session={session}
        />
      </AppFrame>
    );
  }
  return (
    <MainScreen
      conversations={conversations}
      onAddDevice={addDevice}
      onConfigureDevice={configureAgent}
      onCreateConversation={createConversation}
      onLoadDeviceConfig={loadAgentConfig}
      onRefreshDevice={refreshDevice}
      onReleaseDevice={unbindDevice}
      onSend={sendMessage}
      onSignOut={signOut}
      sending={sending}
      session={session}
    />
  );
}

export default function App() {
  return <SafeAreaProvider><AppContent /></SafeAreaProvider>;
}

const styles = StyleSheet.create({
  appFrame: { backgroundColor: colors.background, flex: 1 },
  screen: { backgroundColor: colors.background, flex: 1 },
  fullPage: { backgroundColor: colors.background, flex: 1 },
  mainShell: { backgroundColor: colors.background, flex: 1 },
  bootScreen: { alignItems: 'center', flex: 1, justifyContent: 'center' },
  bootTitle: { color: colors.ink, fontSize: 20, fontWeight: '800', marginBottom: 24, marginTop: 14 },
  brandMark: { alignItems: 'center', backgroundColor: colors.ink, borderRadius: 8, height: 52, justifyContent: 'center', marginBottom: 28, width: 52 },
  brandMarkText: { color: colors.surface, fontSize: 25, fontWeight: '800' },
  authContent: { flexGrow: 1, justifyContent: 'center', padding: 28, paddingBottom: 40 },
  authTopRow: { alignItems: 'flex-start', flexDirection: 'row', justifyContent: 'space-between' },
  authTitle: { color: colors.ink, fontSize: 32, fontWeight: '800', marginBottom: 10 },
  authSubtitle: { color: colors.muted, fontSize: 16, lineHeight: 24, maxWidth: 340 },
  formBlock: { marginTop: 28 },
  fieldGroup: { marginBottom: 17 },
  fieldLabel: { color: colors.ink, fontSize: 14, fontWeight: '700', marginBottom: 8 },
  fieldInput: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, color: colors.ink, fontSize: 16, height: 54, paddingHorizontal: 16 },
  errorText: { color: colors.danger, fontSize: 13, lineHeight: 19, marginBottom: 14 },
  successText: { color: colors.success, fontSize: 13, lineHeight: 19, marginBottom: 14 },
  configLoading: { alignItems: 'center', flexDirection: 'row', minHeight: 42, paddingTop: 12 },
  configLoadingText: { color: colors.muted, fontSize: 13, marginLeft: 10 },
  primaryButton: { alignItems: 'center', backgroundColor: colors.accent, borderRadius: 8, height: 52, justifyContent: 'center', marginTop: 4, paddingHorizontal: 18 },
  primaryButtonText: { color: colors.surface, fontSize: 16, fontWeight: '700' },
  buttonContent: { alignItems: 'center', flexDirection: 'row', justifyContent: 'center' },
  buttonTextWithIcon: { marginLeft: 8 },
  buttonDisabled: { opacity: 0.5 },
  pressed: { opacity: 0.7 },
  rowPressed: { backgroundColor: '#EEF2F6' },
  textButton: { alignItems: 'center', justifyContent: 'center', minHeight: 48, paddingHorizontal: 6 },
  textButtonLabel: { color: colors.accent, fontSize: 14, fontWeight: '700' },
  dangerText: { color: colors.danger },
  authSwitchRow: { alignItems: 'center', flexDirection: 'row', justifyContent: 'center', marginTop: 18 },
  authSwitchHint: { color: colors.muted, fontSize: 14, marginRight: 5 },
  demoBadge: { alignItems: 'center', alignSelf: 'flex-start', backgroundColor: colors.warningSoft, borderRadius: 999, flexDirection: 'row', minHeight: 30, paddingHorizontal: 10 },
  liveBadge: { alignItems: 'center', alignSelf: 'flex-start', backgroundColor: colors.successSoft, borderRadius: 999, flexDirection: 'row', minHeight: 30, paddingHorizontal: 10 },
  demoDot: { backgroundColor: colors.warning, borderRadius: 999, height: 7, marginRight: 7, width: 7 },
  liveDot: { backgroundColor: colors.success, borderRadius: 999, height: 7, marginRight: 7, width: 7 },
  demoBadgeText: { color: colors.warning, fontSize: 12, fontWeight: '700' },
  liveBadgeText: { color: colors.success, fontSize: 12, fontWeight: '700' },
  iconButton: { alignItems: 'center', height: 48, justifyContent: 'center', width: 48 },
  iconButtonPlaceholder: { height: 48, width: 48 },
  pageHeader: { alignItems: 'center', backgroundColor: colors.surface, borderBottomColor: colors.line, borderBottomWidth: StyleSheet.hairlineWidth, flexDirection: 'row', minHeight: 72, paddingHorizontal: 8 },
  pageHeaderCopy: { flex: 1, minWidth: 0, paddingHorizontal: 8 },
  pageHeaderCopyRoot: { paddingLeft: 16 },
  pageTitle: { color: colors.ink, fontSize: 21, fontWeight: '800' },
  pageSubtitle: { color: colors.muted, fontSize: 12, marginTop: 3 },
  pageContent: { flexGrow: 1, padding: 24, paddingBottom: 36 },
  setupIcon: { alignItems: 'center', backgroundColor: colors.accentSoft, borderRadius: 8, height: 76, justifyContent: 'center', marginTop: 18, width: 76 },
  setupTitle: { color: colors.ink, fontSize: 25, fontWeight: '800', marginTop: 24 },
  setupDescription: { color: colors.muted, fontSize: 15, lineHeight: 23, marginTop: 10 },
  stepList: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, marginBottom: 24, marginTop: 24, padding: 16 },
  stepRow: { alignItems: 'center', flexDirection: 'row', minHeight: 44 },
  stepNumber: { backgroundColor: colors.accentSoft, borderRadius: 999, color: colors.accent, fontSize: 13, fontWeight: '800', height: 26, lineHeight: 26, marginRight: 12, textAlign: 'center', width: 26 },
  stepText: { color: colors.ink, flex: 1, fontSize: 14, lineHeight: 20 },
  wifiSectionHeader: { alignItems: 'center', flexDirection: 'row', height: 48, justifyContent: 'space-between' },
  wifiSectionLabel: { color: colors.ink, fontSize: 14, fontWeight: '700' },
  wifiScanning: { alignItems: 'center', backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, flexDirection: 'row', minHeight: 66, paddingHorizontal: 16 },
  wifiScanningText: { color: colors.muted, fontSize: 14, marginLeft: 12 },
  wifiNetworkList: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, marginBottom: 17, overflow: 'hidden' },
  wifiNetworkRow: { alignItems: 'center', flexDirection: 'row', minHeight: 66, paddingHorizontal: 15, paddingVertical: 9 },
  wifiNetworkRowSelected: { backgroundColor: colors.accentSoft },
  wifiNetworkRowDisabled: { opacity: 0.5 },
  wifiNetworkCopy: { flex: 1, marginLeft: 12, minWidth: 0 },
  wifiNetworkName: { color: colors.ink, fontSize: 15, fontWeight: '700' },
  wifiNetworkNameSelected: { color: colors.accent },
  wifiNetworkMeta: { color: colors.subtle, fontSize: 12, marginTop: 4 },
  wifiScanError: { color: colors.warning, fontSize: 13, lineHeight: 19, marginBottom: 4 },
  providerPicker: { marginTop: 24 },
  selectControl: { alignItems: 'center', backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, flexDirection: 'row', minHeight: 54, paddingHorizontal: 16 },
  selectValue: { color: colors.ink, flex: 1, fontSize: 16, fontWeight: '600', marginRight: 10 },
  selectPlaceholder: { color: colors.subtle, flex: 1, fontSize: 16, marginRight: 10 },
  selectMenu: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, marginTop: 8, overflow: 'hidden' },
  selectSearch: { backgroundColor: colors.background, borderBottomColor: colors.line, borderBottomWidth: 1, color: colors.ink, fontSize: 15, height: 52, paddingHorizontal: 15 },
  selectRow: { alignItems: 'center', flexDirection: 'row', minHeight: 58, paddingHorizontal: 15, paddingVertical: 9 },
  selectRowSelected: { backgroundColor: colors.accentSoft },
  selectRowCopy: { flex: 1, marginLeft: 10, minWidth: 0 },
  selectEmpty: { color: colors.muted, fontSize: 14, padding: 16, textAlign: 'center' },
  providerRadio: { borderColor: colors.subtle, borderRadius: 999, borderWidth: 2, height: 18, marginRight: 9, width: 18 },
  providerRadioSelected: { backgroundColor: colors.accent, borderColor: colors.accent, borderWidth: 5 },
  providerLabel: { color: colors.ink, flexShrink: 1, fontSize: 14, fontWeight: '600' },
  providerLabelSelected: { color: colors.accent },
  providerId: { color: colors.subtle, fontSize: 11, marginTop: 3 },
  modelPicker: { marginBottom: 17 },
  modelList: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, overflow: 'hidden' },
  modelRow: { alignItems: 'center', flexDirection: 'row', minHeight: 62, paddingHorizontal: 15, paddingVertical: 9 },
  modelHint: { color: colors.muted, fontSize: 13, lineHeight: 20, marginBottom: 17 },
  formBlockCompact: { marginTop: 22 },
  securityNote: { alignItems: 'center', backgroundColor: colors.successSoft, borderRadius: 8, flexDirection: 'row', marginBottom: 17, minHeight: 52, paddingHorizontal: 14, paddingVertical: 10 },
  securityNoteText: { color: colors.success, flex: 1, fontSize: 13, lineHeight: 19, marginLeft: 10 },
  filterList: { paddingHorizontal: 16, paddingVertical: 12 },
  filterScroll: { flexGrow: 0, maxHeight: 64 },
  filterItem: { alignItems: 'center', backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 999, borderWidth: 1, flexDirection: 'row', height: 40, marginRight: 8, maxWidth: 180, paddingHorizontal: 13 },
  filterItemSelected: { backgroundColor: colors.accentSoft, borderColor: colors.accent },
  filterLabel: { color: colors.muted, flexShrink: 1, fontSize: 13, fontWeight: '600' },
  filterLabelSelected: { color: colors.accent },
  onlineDot: { backgroundColor: colors.success, borderRadius: 999, height: 8, marginRight: 8, width: 8 },
  offlineDot: { backgroundColor: colors.subtle, borderRadius: 999, height: 8, marginRight: 8, width: 8 },
  onlineText: { color: colors.success, fontSize: 12 },
  offlineText: { color: colors.subtle, fontSize: 12 },
  listContent: { flexGrow: 1, padding: 16, paddingBottom: 32 },
  listSurface: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, overflow: 'hidden' },
  conversationRow: { alignItems: 'center', flexDirection: 'row', minHeight: 88, paddingHorizontal: 14, paddingVertical: 12 },
  rowDivider: { borderBottomColor: colors.line, borderBottomWidth: StyleSheet.hairlineWidth },
  conversationIcon: { alignItems: 'center', backgroundColor: colors.accentSoft, borderRadius: 8, height: 44, justifyContent: 'center', marginRight: 12, width: 44 },
  conversationCopy: { flex: 1, marginRight: 8, minWidth: 0 },
  conversationTitleRow: { alignItems: 'center', flexDirection: 'row' },
  rowTitle: { color: colors.ink, flex: 1, fontSize: 16, fontWeight: '700' },
  rowTime: { color: colors.subtle, fontSize: 11, marginLeft: 8 },
  rowPreview: { color: colors.muted, fontSize: 13, lineHeight: 18, marginTop: 6 },
  emptyState: { alignItems: 'stretch', flex: 1, justifyContent: 'center', minHeight: 390, paddingHorizontal: 24 },
  emptyIcon: { alignItems: 'center', alignSelf: 'center', backgroundColor: colors.accentSoft, borderRadius: 8, height: 64, justifyContent: 'center', width: 64 },
  emptyTitle: { color: colors.ink, fontSize: 21, fontWeight: '800', marginTop: 20, textAlign: 'center' },
  emptyText: { color: colors.muted, fontSize: 14, lineHeight: 21, marginBottom: 24, marginTop: 8, textAlign: 'center' },
  messageList: { flexGrow: 1, paddingBottom: 22, paddingHorizontal: 16, paddingTop: 16 },
  dateDividerText: { color: colors.subtle, fontSize: 12, marginBottom: 20, textAlign: 'center' },
  messageGroup: { marginBottom: 16 },
  messageRow: { alignItems: 'flex-end', flexDirection: 'row' },
  userMessageRow: { justifyContent: 'flex-end' },
  agentAvatar: { alignItems: 'center', backgroundColor: colors.ink, borderRadius: 8, height: 30, justifyContent: 'center', marginRight: 8, width: 30 },
  agentAvatarText: { color: colors.surface, fontSize: 14, fontWeight: '800' },
  messageBubble: { borderRadius: 8, maxWidth: '79%', paddingBottom: 9, paddingHorizontal: 14, paddingTop: 12 },
  agentBubble: { backgroundColor: colors.surface, borderBottomLeftRadius: 2 },
  userBubble: { backgroundColor: colors.ink, borderBottomRightRadius: 2 },
  agentMessageText: { color: colors.ink, fontSize: 15, lineHeight: 22 },
  userMessageText: { color: colors.surface, fontSize: 15, lineHeight: 22 },
  agentTime: { color: colors.subtle, fontSize: 10, marginTop: 6 },
  userTime: { color: '#C6D0E0', fontSize: 10, marginTop: 6, textAlign: 'right' },
  messageError: { alignItems: 'flex-start', alignSelf: 'flex-end', flexDirection: 'row', marginTop: 6, maxWidth: '79%' },
  messageErrorText: { color: colors.danger, flexShrink: 1, fontSize: 12, lineHeight: 17, marginLeft: 5, textAlign: 'right' },
  typingBubble: { alignItems: 'center', flexDirection: 'row', paddingBottom: 12 },
  typingText: { color: colors.muted, fontSize: 13, marginLeft: 8 },
  composerError: { backgroundColor: colors.dangerSoft, color: colors.danger, fontSize: 12, lineHeight: 18, paddingHorizontal: 16, paddingVertical: 7 },
  composerWrap: { alignItems: 'flex-end', backgroundColor: colors.surface, borderTopColor: colors.line, borderTopWidth: 1, flexDirection: 'row', paddingHorizontal: 12, paddingVertical: 10 },
  composerInput: { backgroundColor: colors.background, borderColor: colors.line, borderRadius: 8, borderWidth: 1, color: colors.ink, flex: 1, fontSize: 15, maxHeight: 100, minHeight: 48, paddingHorizontal: 14, paddingTop: 12 },
  sendButton: { alignItems: 'center', backgroundColor: colors.accent, borderRadius: 8, height: 48, justifyContent: 'center', marginLeft: 8, width: 48 },
  sendButtonDisabled: { backgroundColor: colors.line },
  deviceRow: { alignItems: 'center', flexDirection: 'row', minHeight: 76, paddingHorizontal: 14, paddingVertical: 12 },
  deviceIcon: { alignItems: 'center', backgroundColor: colors.tealSoft, borderRadius: 8, height: 46, justifyContent: 'center', marginRight: 12, width: 46 },
  deviceRowCopy: { flex: 1, marginRight: 8, minWidth: 0 },
  statusLine: { alignItems: 'center', flexDirection: 'row', marginTop: 6 },
  sectionHint: { color: colors.subtle, fontSize: 12, lineHeight: 18, marginTop: 12, paddingHorizontal: 4 },
  deviceHero: { alignItems: 'center', flexDirection: 'row', marginBottom: 22, marginTop: 8 },
  deviceHeroIcon: { alignItems: 'center', backgroundColor: colors.tealSoft, borderRadius: 8, height: 76, justifyContent: 'center', width: 76 },
  deviceHeroCopy: { flex: 1, marginLeft: 16 },
  deviceName: { color: colors.ink, fontSize: 21, fontWeight: '800' },
  detailSurface: { backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, overflow: 'hidden' },
  detailRow: { alignItems: 'center', borderBottomColor: colors.line, borderBottomWidth: StyleSheet.hairlineWidth, flexDirection: 'row', justifyContent: 'space-between', minHeight: 58, paddingHorizontal: 16, paddingVertical: 10 },
  detailLabel: { color: colors.muted, fontSize: 14 },
  detailValue: { color: colors.ink, flex: 1, fontSize: 14, fontWeight: '600', marginLeft: 22, textAlign: 'right' },
  dangerAction: { alignItems: 'center', borderColor: colors.danger, borderRadius: 8, borderWidth: 1, flexDirection: 'row', height: 52, justifyContent: 'center', marginTop: 24 },
  dangerActionText: { color: colors.danger, fontSize: 15, fontWeight: '700', marginLeft: 8 },
  profileIdentity: { alignItems: 'center', flexDirection: 'row', marginBottom: 28, paddingHorizontal: 4, paddingVertical: 8 },
  profileAvatar: { alignItems: 'center', backgroundColor: colors.ink, borderRadius: 8, height: 58, justifyContent: 'center', width: 58 },
  profileAvatarText: { color: colors.surface, fontSize: 23, fontWeight: '800' },
  profileCopy: { flex: 1, marginLeft: 14, minWidth: 0 },
  profileName: { color: colors.ink, fontSize: 19, fontWeight: '800' },
  profileEmail: { color: colors.muted, fontSize: 13, marginTop: 5 },
  listSectionTitle: { color: colors.muted, fontSize: 13, fontWeight: '700', marginBottom: 8, marginLeft: 4, marginTop: 18 },
  settingsRow: { alignItems: 'center', flexDirection: 'row', minHeight: 58, paddingHorizontal: 14 },
  settingsDivider: { backgroundColor: colors.line, height: StyleSheet.hairlineWidth, marginLeft: 51 },
  settingsLabel: { color: colors.ink, flex: 1, fontSize: 15, marginLeft: 14 },
  settingsValue: { color: colors.subtle, fontSize: 13, marginRight: 8, maxWidth: 120 },
  signOutButton: { alignItems: 'center', backgroundColor: colors.surface, borderColor: colors.line, borderRadius: 8, borderWidth: 1, flexDirection: 'row', height: 52, justifyContent: 'center', marginTop: 28 },
  signOutText: { color: colors.danger, fontSize: 15, fontWeight: '700', marginLeft: 8 },
  supportIcon: { alignItems: 'center', backgroundColor: colors.accentSoft, borderRadius: 8, height: 72, justifyContent: 'center', marginTop: 12, width: 72 },
  detailTitle: { color: colors.ink, fontSize: 24, fontWeight: '800', marginTop: 22 },
  detailBody: { color: colors.muted, fontSize: 14, lineHeight: 22, marginBottom: 24, marginTop: 8 },
  articleText: { color: colors.ink, fontSize: 15, lineHeight: 25, marginTop: 20 },
  aboutMark: { alignItems: 'center', backgroundColor: colors.ink, borderRadius: 8, height: 72, justifyContent: 'center', marginTop: 12, width: 72 },
  bottomNav: { backgroundColor: colors.surface, borderTopColor: colors.line, borderTopWidth: 1, flexDirection: 'row', minHeight: 68, paddingBottom: 3 },
  navItem: { alignItems: 'center', flex: 1, justifyContent: 'center', minHeight: 62 },
  navLabel: { color: colors.subtle, fontSize: 12, fontWeight: '600', marginTop: 4 },
  navLabelSelected: { color: colors.accent, fontWeight: '800' },
});
