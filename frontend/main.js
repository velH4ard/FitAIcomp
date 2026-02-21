import "./styles.css";
import {
  ApiError,
  authTelegram,
  createYookassaPayment,
  refreshYookassaPayment,
  getMe,
  getMealById,
  getMeals,
  getMonthlyReport,
  getSubscription,
  getWeeklyReport,
  getWeightChart,
  getWhyNotLosing,
  patchNotificationSettings,
  getStatsDaily,
  getUsageToday,
  getStreak,
  analyzeMeal,
  setTokenGetter,
  setSilentReauthHandler,
  setTokenInvalidator,
  setUnauthorizedHandler,
  updateProfileGoal,
  updateProfile,
} from "./api";
import {
  createFormField,
  createHeaderBackButton,
  createHeaderShell,
  createHistoryList,
  createPrimaryButton,
  createProgressBar,
  createQuotaLabel,
  createPremiumGateCard,
  createResultCard,
  createRoot,
  createSecondaryButton,
  createSubscriptionHint,
  createStreakBadge,
  createStreakModal,
  createShareCard,
  QUOTES_ALL,
  QUOTES_SHARE_SHORT,
  getLastShownQuoteIndex,
  pickRandomQuote,
  rememberQuoteOverlayShown,
  renderQuoteOverlay,
  showToast,
  shouldShowQuoteOverlay,
} from "./ui";

const app = document.getElementById("app");
const appHeader = document.getElementById("app-header");
const appContent = document.getElementById("app-content");
const SHARE_PATH = "/share";
const STORAGE_TOKEN_KEY = "fitai.accessToken";
const STORAGE_PENDING_PAYMENT_KEY = "fitai.pendingPaymentId";
const MEAL_DESCRIPTION_MAX_LENGTH = 500;
const APP_ENV = import.meta.env.VITE_APP_ENV ?? import.meta.env.MODE;
const IS_DEV = APP_ENV !== "production";
const TOKEN_REFRESH_EARLY_MS = 5 * 60 * 1000;
const AUTH_RETRY_COOLDOWN_MS = 30 * 1000;
const REMINDER_TONE_DEFAULT = "balanced";
const PREMIUM_ANALYTICS_SCREENS = new Set([
  "weeklyReport",
  "monthlyReport",
  "whyNotLosing",
  "weightChart",
]);

const state = {
  token: localStorage.getItem(STORAGE_TOKEN_KEY),
  user: null,
  usage: null,
  subscription: null,
  dailyStats: null,
  streak: null,
  streakModalOpen: false,
  pendingPaymentId: localStorage.getItem(STORAGE_PENDING_PAYMENT_KEY),
  history: [],
  lastAnalyzeResponse: null,
  selectedMeal: null,
  mealDescription: "",
  mealDescriptionError: "",
  mealDescriptionExpanded: false,
  screen: "loading",
  busy: false,
  analyzing: false,
  scrollByScreen: {},
  currentScreen: "loading",
  shareData: null,
  lastSubmittedDescription: "",
  quoteOverlayPending: true,
  quoteOverlayScheduled: false,
  quoteOverlayVisible: false,
  loadingText: "Подключаемся к серверу",
  authErrorMessage: "",
  reminderEnabled: null,
  reminderTone: REMINDER_TONE_DEFAULT,
  reminderSaving: false,
  weeklyReport: null,
  monthlyReport: null,
  whyNotLosing: null,
  weightChart: null,
  analyticsLoading: false,
  analyticsError: "",
  premiumGateReason: "",
  goalDraft: "",
  goalSaving: false,
};

let tokenRefreshTimer = null;
let silentReauthPromise = null;
let authFlowPromise = null;
let nextAutoAuthAttemptAt = 0;

function devLog(message) {
  if (IS_DEV) {
    console.info(message);
  }
}

function clearTokenRefreshTimer() {
  if (tokenRefreshTimer !== null) {
    window.clearTimeout(tokenRefreshTimer);
    tokenRefreshTimer = null;
  }
}

function decodeJwtPayload(token) {
  if (!token || typeof token !== "string") {
    return null;
  }

  const parts = token.split(".");
  if (parts.length < 2) {
    return null;
  }

  const base64Url = parts[1];
  const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");

  try {
    const json = window.atob(padded);
    return JSON.parse(json);
  } catch (_error) {
    return null;
  }
}

function clearTokenOnly() {
  state.token = null;
  localStorage.removeItem(STORAGE_TOKEN_KEY);
  clearTokenRefreshTimer();
}

function scheduleTokenRefresh(token) {
  clearTokenRefreshTimer();

  const payload = decodeJwtPayload(token);
  const exp = Number(payload?.exp);
  if (!Number.isFinite(exp) || exp <= 0) {
    return;
  }

  const refreshAtMs = exp * 1000 - TOKEN_REFRESH_EARLY_MS;
  const delay = refreshAtMs - Date.now();

  const runRefresh = () => {
    silentTelegramReauth({ reason: "scheduled" });
  };

  if (delay <= 0) {
    tokenRefreshTimer = window.setTimeout(runRefresh, 0);
    return;
  }

  tokenRefreshTimer = window.setTimeout(runRefresh, delay);
}

async function silentTelegramReauth() {
  if (silentReauthPromise) {
    return silentReauthPromise;
  }

  silentReauthPromise = (async () => {
    const initData = getTelegramInitData();
    if (!initData) {
      return false;
    }

    clearTokenOnly();

    try {
      const response = await authTelegram(initData);
      const nextToken = response?.accessToken;
      if (!nextToken) {
        return false;
      }
      saveToken(nextToken);
      devLog("token refreshed");
      return true;
    } catch (_error) {
      return false;
    }
  })();

  try {
    return await silentReauthPromise;
  } finally {
    silentReauthPromise = null;
  }
}

setTokenGetter(() => state.token);
setTokenInvalidator(() => {
  clearTokenOnly();
});
setSilentReauthHandler(silentTelegramReauth);
setUnauthorizedHandler(() => {
  if (state.screen === "auth" || state.screen === "loading") {
    return;
  }
  clearSession();
  bootstrapAuth({ silent: true });
});

if (state.token) {
  scheduleTokenRefresh(state.token);
}

const tgWebApp = window.Telegram?.WebApp;

function isSharePath() {
  return window.location.pathname === SHARE_PATH;
}

let suppressNextHistoryPush = false;

function syncBrowserHistory(previousScreen) {
  if (state.screen === "loading") {
    return;
  }

  const targetPath = state.screen === "share" ? SHARE_PATH : "/";
  const currentState = window.history.state;
  const hasKnownState = typeof currentState?.screen === "string";

  if (!hasKnownState) {
    window.history.replaceState({ screen: state.screen }, "", targetPath);
    return;
  }

  const screenChanged = previousScreen !== state.screen;
  if (screenChanged && !suppressNextHistoryPush) {
    window.history.pushState({ screen: state.screen }, "", targetPath);
    return;
  }

  if (window.location.pathname !== targetPath || currentState.screen !== state.screen) {
    window.history.replaceState({ screen: state.screen }, "", targetPath);
  }
}

function leaveShareScreen() {
  state.screen = "main";
  render();
}

function syncTelegramBackButton() {
  if (!tgWebApp?.BackButton) {
    return;
  }
  tgWebApp.BackButton.offClick(leaveShareScreen);
  tgWebApp.BackButton.hide();
}

function getTelegramInitData() {
  return tgWebApp?.initData || "";
}

function notifyTelegramAppReady() {
  if (!tgWebApp) {
    return;
  }

  if (typeof tgWebApp.ready === "function") {
    tgWebApp.ready();
  }
  if (typeof tgWebApp.expand === "function") {
    tgWebApp.expand();
  }
}

function clearSession() {
  clearTokenOnly();
  state.user = null;
  state.usage = null;
  state.subscription = null;
  state.dailyStats = null;
  state.pendingPaymentId = null;
  localStorage.removeItem(STORAGE_PENDING_PAYMENT_KEY);
}

function setPendingPaymentId(paymentId) {
  state.pendingPaymentId = paymentId || null;
  if (paymentId) {
    localStorage.setItem(STORAGE_PENDING_PAYMENT_KEY, paymentId);
  } else {
    localStorage.removeItem(STORAGE_PENDING_PAYMENT_KEY);
  }
}

function getTodayUtcDate() {
  return new Date().toISOString().slice(0, 10);
}

function saveToken(token) {
  state.token = token;
  localStorage.setItem(STORAGE_TOKEN_KEY, token);
  scheduleTokenRefresh(token);
}

function mapFriendlyError(error) {
  if (!(error instanceof ApiError)) {
    return "Попробуйте позже";
  }

  const messages = {
    NETWORK: "Проблема соединения",
    NETWORK_ERROR: "Проблема соединения",
    VALIDATION_FAILED: "Проверьте корректность данных",
    AI_PROVIDER_ERROR: "Не удалось распознать блюдо",
    STORAGE_ERROR: "Ошибка загрузки фото",
    PAYMENT_PROVIDER_ERROR: "Не удалось проверить платеж",
    PAYWALL_BLOCKED: "Функция доступна в Premium",
    RATE_LIMITED: "Слишком много запросов",
    INTERNAL_ERROR: "Попробуйте позже",
  };

  return messages[error.code] || "Попробуйте позже";
}

function validateMealDescription(value) {
  const length = String(value || "").length;
  if (length > MEAL_DESCRIPTION_MAX_LENGTH) {
    return `Максимум ${MEAL_DESCRIPTION_MAX_LENGTH} символов`;
  }
  return "";
}

function getTrimmedMealDescription() {
  const value = String(state.mealDescription || "").trim();
  return value || "";
}

function hasNonEmptyText(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function hasMealDescription(meal, source) {
  const candidates = [meal?.description, meal?.meal?.description, meal?.result?.description];
  if (candidates.some(hasNonEmptyText)) {
    return true;
  }

  if (source === "last" && hasNonEmptyText(state.lastSubmittedDescription)) {
    return true;
  }

  return false;
}

function getDescriptionBackendError(error) {
  if (!(error instanceof ApiError) || error.code !== "VALIDATION_FAILED") {
    return "";
  }

  const fieldErrors = Array.isArray(error.details?.fieldErrors) ? error.details.fieldErrors : [];
  const descriptionError = fieldErrors.find((item) => {
    const field = String(item?.field || "").toLowerCase();
    return field.includes("description");
  });

  if (!descriptionError) {
    return "";
  }

  const issue = String(descriptionError.issue || "").trim();
  if (!issue) {
    return `Максимум ${MEAL_DESCRIPTION_MAX_LENGTH} символов`;
  }
  if (issue.includes("500")) {
    return `Максимум ${MEAL_DESCRIPTION_MAX_LENGTH} символов`;
  }
  return "Некорректное описание блюда";
}

function isMissingImageValidationError(error) {
  if (!(error instanceof ApiError) || error.code !== "VALIDATION_FAILED") {
    return false;
  }

  const fieldErrors = Array.isArray(error.details?.fieldErrors) ? error.details.fieldErrors : [];
  return fieldErrors.some((item) => {
    const field = String(item?.field || "").toLowerCase();
    const issue = String(item?.issue || "").toLowerCase();
    if (!field.includes("image") && !field.includes("file")) {
      return false;
    }
    return issue.includes("required") || issue.includes("missing") || issue.includes("empty");
  });
}

function mapAnalyzeErrorToToast(error) {
  if (!(error instanceof ApiError)) {
    return "Попробуйте позже";
  }

  const messages = {
    NETWORK_ERROR: "Проблема соединения",
    IDEMPOTENCY_CONFLICT: "Запрос уже обрабатывается",
    RATE_LIMITED: "Слишком много запросов, попробуйте позже",
    AI_PROVIDER_ERROR: "Не удалось распознать блюдо",
    STORAGE_ERROR: "Ошибка загрузки фото, попробуйте позже",
  };

  return messages[error.code] || "Попробуйте позже";
}

function routeByBusinessError(error) {
  if (!(error instanceof ApiError)) {
    return false;
  }
  if (error.code === "UNAUTHORIZED") {
    clearSession();
    bootstrapAuth({ silent: true });
    return true;
  }
  if (error.code === "ONBOARDING_REQUIRED") {
    state.screen = "onboarding";
    render();
    return true;
  }
  if (error.code === "QUOTA_EXCEEDED") {
    state.screen = "paywall";
    render();
    return true;
  }
  if (error.code === "PAYWALL_BLOCKED") {
    if (PREMIUM_ANALYTICS_SCREENS.has(state.screen)) {
      state.premiumGateReason = "Для этой функции нужен активный Premium.";
    } else {
      state.screen = "paywall";
    }
    render();
    return true;
  }
  return false;
}

function setBusy(value) {
  if (state.busy === value) {
    return;
  }
  state.busy = value;
  render();
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, value));
}

function toNumber(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function getDailyTarget(profile) {
  const targets = {
    lose_weight: 1800,
    maintain: 2200,
    gain_weight: 2600,
  };
  return targets[profile?.goal] ?? 2200;
}

function formatDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return date.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

function formatDayDate(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
  });
}

function formatMetric(value, digits = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) {
    return "0";
  }
  return n.toLocaleString("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function buildChartPath(points, width, height, padding) {
  if (!points.length) {
    return "";
  }

  const values = points.map((point) => Number(point.weight) || 0);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(1, max - min);
  const step = points.length > 1 ? (width - padding * 2) / (points.length - 1) : 0;

  return points.map((point, index) => {
    const x = padding + step * index;
    const y = height - padding - (((Number(point.weight) || 0) - min) / span) * (height - padding * 2);
    const cmd = index === 0 ? "M" : "L";
    return `${cmd}${x.toFixed(2)} ${y.toFixed(2)}`;
  }).join(" ");
}

function readReminderEnabled(source) {
  const candidates = [
    source?.notificationSettings?.enabled,
    source?.notificationSettings?.calorieReminderEnabled,
    source?.notifications?.enabled,
    source?.notifications?.calorieReminderEnabled,
    source?.enabled,
    source?.reminderEnabled,
  ];

  for (const value of candidates) {
    if (typeof value === "boolean") {
      return value;
    }
  }

  return null;
}

function normalizeReminderTone(value) {
  const tone = String(value || "").trim().toLowerCase();
  if (tone === "soft" || tone === "balanced" || tone === "hard") {
    return tone;
  }
  return null;
}

function readReminderTone(source) {
  const candidates = [
    source?.notificationSettings?.tone,
    source?.notificationSettings?.reminderTone,
    source?.notifications?.tone,
    source?.notifications?.reminderTone,
    source?.tone,
    source?.reminderTone,
  ];

  for (const value of candidates) {
    const tone = normalizeReminderTone(value);
    if (tone) {
      return tone;
    }
  }

  return null;
}

function syncReminderStateFrom(source) {
  const enabled = readReminderEnabled(source);
  if (typeof enabled === "boolean") {
    state.reminderEnabled = enabled;
  }

  const tone = readReminderTone(source);
  if (tone) {
    state.reminderTone = tone;
  }
}

function ensureReminderState() {
  if (typeof state.reminderEnabled !== "boolean") {
    syncReminderStateFrom(state.user);
  }
  if (typeof state.reminderEnabled !== "boolean") {
    state.reminderEnabled = false;
  }

  if (!normalizeReminderTone(state.reminderTone)) {
    state.reminderTone = REMINDER_TONE_DEFAULT;
  }
}

function openPremiumEntryPoint() {
  const status = state.subscription?.status;
  state.screen = status === "active" ? "subscription" : "paywall";
  render();
}

function isPremiumActive() {
  return state.subscription?.status === "active";
}

function createPremiumGateSection(note = "") {
  const root = createRoot();
  root.append(createPremiumGateCard({
    onCta: handleUpgrade,
    note,
  }));
  return root;
}

function createPremiumHeaderButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-sm rounded-full border border-violet-200 bg-violet-100 px-4 text-violet-700 shadow-sm transition-all duration-150 hover:border-violet-300 hover:bg-violet-200";
  button.textContent = "Premium";
  button.addEventListener("click", openPremiumEntryPoint);
  return button;
}

function openStreakModal() {
  state.streakModalOpen = true;
  render();
}

function closeStreakModal() {
  state.streakModalOpen = false;
  render();
}

function maybeShowQuoteOverlay() {
  if (!state.quoteOverlayPending || state.quoteOverlayScheduled || state.quoteOverlayVisible) {
    return;
  }

  if (state.screen !== "main") {
    return;
  }

  if (!state.token || !state.user?.isOnboarded || state.busy || state.analyzing || state.streakModalOpen) {
    return;
  }

  state.quoteOverlayPending = false;
  if (!shouldShowQuoteOverlay()) {
    return;
  }

  const lastIndex = getLastShownQuoteIndex();
  const picked = pickRandomQuote(QUOTES_ALL, lastIndex);
  if (!picked.text) {
    return;
  }

  state.quoteOverlayScheduled = true;
  const run = () => {
    state.quoteOverlayScheduled = false;

    if (state.screen !== "main" || state.busy || state.analyzing || state.streakModalOpen || state.quoteOverlayVisible) {
      return;
    }

    const existing = document.querySelector(".quote-modal-overlay");
    if (existing) {
      existing.remove();
    }

    state.quoteOverlayVisible = true;
    const overlay = renderQuoteOverlay(picked.text, () => {
      rememberQuoteOverlayShown(picked.index);
      state.quoteOverlayVisible = false;
    });
    app?.append(overlay);
  };

  if (typeof window.requestIdleCallback === "function") {
    window.requestIdleCallback(run, { timeout: 600 });
  } else {
    window.setTimeout(run, 220);
  }
}

function createDailySummaryCard() {
  const card = document.createElement("section");
  card.className = "rounded-2xl bg-white p-5 shadow-sm";

  const consumed = Math.round(state.dailyStats?.calories_kcal ?? 0);
  const target = getDailyTarget(state.user?.profile);
  const remaining = Math.max(0, target - consumed);
  const progress = clampPercent((consumed / Math.max(1, target)) * 100);

  const label = document.createElement("p");
  label.className = "text-xs text-slate-500";
  label.textContent = "Дневной итог";

  const title = document.createElement("p");
  title.className = "mt-2 text-3xl font-semibold tracking-tight text-slate-900";
  title.textContent = `${consumed} ккал`;

  const targetLabel = document.createElement("p");
  targetLabel.className = "mt-2 text-sm text-slate-700";
  targetLabel.textContent = `Сегодня: ${consumed} / ${target} ккал`;

  const track = createProgressBar({
    value: progress,
    max: 100,
    label: "Прогресс по дневной цели",
  });

  const bottom = document.createElement("div");
  bottom.className = "mt-3 flex items-center justify-between gap-2";

  const remainingLabel = document.createElement("span");
  remainingLabel.className = "text-sm text-slate-600";
  remainingLabel.textContent = `Осталось ${remaining} ккал`;

  const photosLeft = document.createElement("span");
  photosLeft.className = "rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700";
  const remainingPhotos = state.usage?.remaining ?? state.subscription?.remainingToday ?? 0;
  const limitPhotos = state.usage?.dailyLimit ?? state.subscription?.dailyLimit ?? 0;
  photosLeft.textContent = `${remainingPhotos} из ${limitPhotos} фото`;

  bottom.append(remainingLabel, photosLeft);
  card.append(label, title, targetLabel, track, bottom);

  if (state.subscription?.status === "active" && state.subscription?.activeUntil) {
    const premium = document.createElement("div");
    premium.className = "mt-3 inline-flex w-fit items-center rounded-full bg-violet-100 px-3 py-1 text-xs font-medium text-violet-700";
    premium.textContent = `Премиум активен до ${formatDate(state.subscription.activeUntil)}`;
    card.append(premium);
  }

  return card;
}

async function bootstrapAuth(options = {}) {
  const { silent = false } = options;
  if (authFlowPromise) {
    return authFlowPromise;
  }

  if (silent && Date.now() < nextAutoAuthAttemptAt) {
    return false;
  }

  authFlowPromise = (async () => {
  const initData = getTelegramInitData();
  if (!initData) {
    clearSession();
    state.authErrorMessage = "";
    state.screen = "auth";
    render();
    return false;
  }

  state.authErrorMessage = "";
  state.loadingText = "Авторизация...";
  state.screen = "loading";
  render();
  setBusy(true);
  try {
    const response = await authTelegram(initData);
    saveToken(response.accessToken);
    nextAutoAuthAttemptAt = 0;
    notifyTelegramAppReady();
    await bootstrapUser();
    return true;
  } catch (error) {
    clearSession();
    nextAutoAuthAttemptAt = Date.now() + AUTH_RETRY_COOLDOWN_MS;
    state.authErrorMessage = "Ошибка авторизации. Откройте приложение через Telegram.";
    state.screen = "auth";
    render();
    return false;
  } finally {
    state.loadingText = "Подключаемся к серверу";
    setBusy(false);
  }
  })();

  try {
    return await authFlowPromise;
  } finally {
    authFlowPromise = null;
  }
}

async function bootstrapUser() {
  if (!state.token) {
    state.screen = "auth";
    render();
    return;
  }

  setBusy(true);
  try {
    state.user = await getMe();
    syncReminderStateFrom(state.user);
    await refreshUsageAndSubscription();
    const shouldOpenShareFromRoute = isSharePath();

    if (!state.user.isOnboarded) {
      state.screen = "onboarding";
    } else if (state.usage?.remaining === 0 && state.usage?.subscriptionStatus !== "active") {
      state.screen = "paywall";
    } else if (shouldOpenShareFromRoute) {
      state.screen = "main";
      await openShareScreen({ pushRoute: false });
    } else {
      state.screen = "main";
    }
  } catch (error) {
    if (!routeByBusinessError(error)) {
      showToast(mapFriendlyError(error));
    }
  } finally {
    setBusy(false);
    render();
  }
}

async function refreshUsageAndSubscription() {
  if (!state.token) {
    return;
  }
  const [usage, subscription, dailyStats, streak] = await Promise.all([
    getUsageToday(),
    getSubscription(),
    getStatsDaily(getTodayUtcDate()),
    getStreak().catch(() => ({ currentStreak: 0, bestStreak: 0, lastCompletedDate: null })),
  ]);
  state.usage = usage;
  state.subscription = subscription;
  state.dailyStats = dailyStats;
  state.streak = streak;
}

async function submitOnboarding(formValues) {
  setBusy(true);
  try {
    await updateProfile(formValues);
    state.user = await getMe();
    syncReminderStateFrom(state.user);
    await refreshUsageAndSubscription();
    state.screen = "main";
  } catch (error) {
    if (!routeByBusinessError(error)) {
      showToast(mapFriendlyError(error));
    }
  } finally {
    setBusy(false);
    render();
  }
}

async function handleReminderToggle(nextEnabled) {
  if (state.reminderSaving) {
    return;
  }

  const previousEnabled = Boolean(state.reminderEnabled);
  const previousTone = normalizeReminderTone(state.reminderTone) || REMINDER_TONE_DEFAULT;
  state.reminderEnabled = Boolean(nextEnabled);
  state.reminderSaving = true;
  render();

  try {
    const response = await patchNotificationSettings({
      enabled: state.reminderEnabled,
      tone: normalizeReminderTone(state.reminderTone) || REMINDER_TONE_DEFAULT,
    });
    syncReminderStateFrom(response);
    if (!readReminderTone(response)) {
      state.reminderTone = REMINDER_TONE_DEFAULT;
    }
    ensureReminderState();
    showToast("Настройки уведомлений сохранены", "info");
  } catch (error) {
    state.reminderEnabled = previousEnabled;
    state.reminderTone = previousTone;
    showToast(mapFriendlyError(error));
  } finally {
    state.reminderSaving = false;
    render();
  }
}

async function handleReminderToneChange(nextTone) {
  const normalizedTone = normalizeReminderTone(nextTone) || REMINDER_TONE_DEFAULT;
  if (state.reminderSaving || state.reminderTone === normalizedTone) {
    return;
  }

  const previousEnabled = Boolean(state.reminderEnabled);
  const previousTone = normalizeReminderTone(state.reminderTone) || REMINDER_TONE_DEFAULT;
  state.reminderTone = normalizedTone;
  state.reminderSaving = true;
  render();

  try {
    const response = await patchNotificationSettings({
      enabled: state.reminderEnabled,
      tone: normalizedTone,
    });
    syncReminderStateFrom(response);
    if (!readReminderTone(response)) {
      state.reminderTone = REMINDER_TONE_DEFAULT;
    }
    ensureReminderState();
    showToast("Настройки уведомлений сохранены", "info");
  } catch (error) {
    state.reminderEnabled = previousEnabled;
    state.reminderTone = previousTone;
    showToast(mapFriendlyError(error));
  } finally {
    state.reminderSaving = false;
    render();
  }
}

async function handleAnalyze(file) {
  if (!file) {
    return;
  }

  const descriptionError = validateMealDescription(state.mealDescription);
  if (descriptionError) {
    state.mealDescriptionError = descriptionError;
    state.mealDescriptionExpanded = true;
    render();
    return;
  }

  state.mealDescriptionError = "";
  const mealDescription = getTrimmedMealDescription();
  state.lastSubmittedDescription = mealDescription;

  if (state.analyzing) {
    return;
  }

  if (state.usage && state.usage.remaining === 0 && state.usage.subscriptionStatus !== "active") {
    state.screen = "paywall";
    render();
    return;
  }

  const previousScreen = state.screen;
  setBusy(true);
  state.analyzing = true;
  state.screen = "result";
  render();
  try {
    const response = await analyzeMeal(file, {
      description: mealDescription,
    });
    state.lastAnalyzeResponse = response;
    state.usage = response.usage;
    state.mealDescription = "";
    state.mealDescriptionError = "";
    state.mealDescriptionExpanded = false;

    // Optimistic daily summary update so the main screen reflects calories immediately.
    const prev = state.dailyStats || {};
    const totals = response?.meal?.result?.totals || {};
    const prevMealsCount = toNumber(prev.mealsCount ?? prev.meals_count, 0);
    const prevCalories = toNumber(prev.calories_kcal, 0);
    const prevProtein = toNumber(prev.protein_g, 0);
    const prevFat = toNumber(prev.fat_g, 0);
    const prevCarbs = toNumber(prev.carbs_g, 0);

    const addedCalories = toNumber(totals.calories_kcal, 0);
    const addedProtein = toNumber(totals.protein_g, 0);
    const addedFat = toNumber(totals.fat_g, 0);
    const addedCarbs = toNumber(totals.carbs_g, 0);

    const optimisticStats = {
      date: getTodayUtcDate(),
      calories_kcal: prevCalories + addedCalories,
      protein_g: prevProtein + addedProtein,
      fat_g: prevFat + addedFat,
      carbs_g: prevCarbs + addedCarbs,
      mealsCount: prevMealsCount + 1,
    };
    state.dailyStats = optimisticStats;

    try {
      const fresh = await getStatsDaily(getTodayUtcDate());
      const freshCalories = toNumber(fresh?.calories_kcal, 0);
      const freshMealsCount = toNumber(fresh?.mealsCount ?? fresh?.meals_count, 0);

      // Keep optimistic numbers if backend summary lags behind for a moment.
      if (
        freshCalories >= optimisticStats.calories_kcal
        || freshMealsCount >= optimisticStats.mealsCount
      ) {
        state.dailyStats = fresh;
      }
    } catch (_error) {
      // Keep optimistic stats if refresh fails.
    }

    state.screen = "result";
  } catch (error) {
    const backendDescriptionError = getDescriptionBackendError(error);
    if (backendDescriptionError) {
      state.mealDescriptionError = backendDescriptionError;
      state.mealDescriptionExpanded = true;
    }

    if (!routeByBusinessError(error)) {
      state.screen = previousScreen;
      if (!backendDescriptionError && isMissingImageValidationError(error)) {
        showToast("Добавьте фото");
      } else if (!backendDescriptionError) {
        showToast(mapAnalyzeErrorToToast(error));
      }
    }
  } finally {
    state.analyzing = false;
    setBusy(false);
    render();
  }
}

function createAnalyzeSkeletonScreen() {
  const section = document.createElement("section");
  section.className = "flex flex-col gap-4";

  const hero = document.createElement("section");
  hero.className = "rounded-2xl bg-white p-5 shadow-sm";

  const heroTitle = document.createElement("div");
  heroTitle.className = "skeleton-shimmer skeleton-line h-3 w-24 rounded-md";

  const heroValue = document.createElement("div");
  heroValue.className = "skeleton-shimmer skeleton-line mt-3 h-8 w-32 rounded-lg";

  const heroCaption = document.createElement("div");
  heroCaption.className = "skeleton-shimmer skeleton-line mt-3 h-3 w-40 rounded-md";

  const macros = document.createElement("div");
  macros.className = "mt-4 grid grid-cols-3 gap-2";
  for (let i = 0; i < 3; i += 1) {
    const macro = document.createElement("div");
    macro.className = "rounded-xl border border-slate-100 bg-white p-3 shadow-sm";

    const macroLabel = document.createElement("div");
    macroLabel.className = "skeleton-shimmer skeleton-line h-3 w-12 rounded-md";

    const macroValue = document.createElement("div");
    macroValue.className = "skeleton-shimmer skeleton-line mt-2 h-4 w-16 rounded-md";

    macro.append(macroLabel, macroValue);
    macros.append(macro);
  }

  hero.append(heroTitle, heroValue, heroCaption, macros);

  const list = document.createElement("div");
  list.className = "flex flex-col gap-3";
  for (let i = 0; i < 2; i += 1) {
    const card = document.createElement("article");
    card.className = "rounded-2xl border border-slate-100 bg-white p-4 shadow-sm";

    const lineTop = document.createElement("div");
    lineTop.className = "flex items-center justify-between gap-2";

    const left = document.createElement("div");
    left.className = "skeleton-shimmer skeleton-line h-4 w-28 rounded-md";

    const right = document.createElement("div");
    right.className = "skeleton-shimmer skeleton-line h-6 w-16 rounded-full";

    lineTop.append(left, right);

    const lineBottom = document.createElement("div");
    lineBottom.className = "skeleton-shimmer skeleton-line mt-3 h-3 w-44 rounded-md";

    card.append(lineTop, lineBottom);
    list.append(card);
  }

  const loadingText = document.createElement("p");
  loadingText.className = "text-xs text-slate-500";
  loadingText.textContent = "Анализируем фото...";

  section.append(hero, list, loadingText);
  return section;
}

async function openHistory() {
  setBusy(true);
  try {
    const data = await getMeals(20);
    state.history = data.items || [];
    state.screen = "history";
  } catch (error) {
    showToast(mapFriendlyError(error));
  } finally {
    setBusy(false);
    render();
  }
}

async function openMeal(mealId) {
  setBusy(true);
  try {
    const meal = await getMealById(mealId);
    state.selectedMeal = meal;
    state.screen = "historyDetail";
  } catch (error) {
    showToast(mapFriendlyError(error));
  } finally {
    setBusy(false);
    render();
  }
}

async function openAnalyticsScreen(screen) {
  state.analyticsError = "";
  state.premiumGateReason = "";
  state.screen = screen;

  if (!isPremiumActive()) {
    render();
    return;
  }

  state.analyticsLoading = true;
  render();

  try {
    if (screen === "weeklyReport") {
      state.weeklyReport = await getWeeklyReport();
    }
    if (screen === "monthlyReport") {
      state.monthlyReport = await getMonthlyReport();
    }
    if (screen === "whyNotLosing") {
      state.whyNotLosing = await getWhyNotLosing();
      const fromPayload = Number(state.whyNotLosing?.dailyGoalKcal ?? state.whyNotLosing?.dailyGoal ?? state.whyNotLosing?.goalCalories_kcal);
      if (Number.isFinite(fromPayload) && fromPayload > 0) {
        state.goalDraft = String(Math.round(fromPayload));
      } else if (!state.goalDraft) {
        state.goalDraft = String(getDailyTarget(state.user?.profile));
      }
    }
    if (screen === "weightChart") {
      state.weightChart = await getWeightChart();
    }
  } catch (error) {
    if (error instanceof ApiError && error.code === "PAYWALL_BLOCKED") {
      state.premiumGateReason = "Для этой функции нужен активный Premium.";
    } else {
      state.analyticsError = mapFriendlyError(error);
    }
  } finally {
    state.analyticsLoading = false;
    render();
  }
}

async function saveGoalFromWhyNotLosing() {
  if (!isPremiumActive() || state.goalSaving) {
    return;
  }
  const nextGoal = Number.parseInt(String(state.goalDraft || ""), 10);
  if (!Number.isInteger(nextGoal)) {
    showToast("Введите корректную цель в ккал");
    return;
  }

  state.goalSaving = true;
  render();
  try {
    const response = await updateProfileGoal(nextGoal);
    state.goalDraft = String(response?.dailyGoal ?? nextGoal);
    showToast("Цель обновлена", "info");
  } catch (error) {
    showToast(mapFriendlyError(error));
  } finally {
    state.goalSaving = false;
    render();
  }
}

async function handleUpgrade() {
  setBusy(true);
  try {
    const result = await createYookassaPayment(window.location.href);
    if (result?.paymentId) {
      setPendingPaymentId(result.paymentId);
    }
    if (result?.confirmationUrl) {
      window.location.href = result.confirmationUrl;
      return;
    }
    showToast("Не удалось получить ссылку оплаты");
  } catch (error) {
    showToast(mapFriendlyError(error));
  } finally {
    setBusy(false);
    render();
  }
}

async function refreshAfterResume({ notifyResult = false } = {}) {
  if (!state.token) {
    return;
  }

  try {
    let refreshAttempted = false;
    let refreshFailed = false;
    let refreshError = null;

    if (state.pendingPaymentId) {
      refreshAttempted = true;
      try {
        await refreshYookassaPayment(state.pendingPaymentId);
      } catch (error) {
        refreshFailed = true;
        refreshError = error;
      }
    }

    const previousStatus = state.subscription?.status;
    await refreshUsageAndSubscription();
    if (previousStatus !== "active" && state.subscription?.status === "active") {
      showToast("Премиум активирован", "info");
      setPendingPaymentId(null);
      if (state.screen === "paywall") {
        state.screen = "main";
      }
    } else if (notifyResult && refreshFailed && refreshError) {
      showToast(mapFriendlyError(refreshError), "warning");
    } else if (notifyResult && state.pendingPaymentId && refreshAttempted && !refreshFailed) {
      showToast("Платеж еще обрабатывается. Повторите проверку через минуту.", "warning");
    }
    if (state.usage?.remaining === 0 && state.usage?.subscriptionStatus !== "active" && state.screen === "main") {
      state.screen = "paywall";
    }
    render();
  } catch (error) {
    if (notifyResult) {
      showToast(mapFriendlyError(error), "warning");
    }
  }
}

function renderLoadingScreen() {
  const root = createRoot();
  const loadingCard = document.createElement("section");
  loadingCard.className = "rounded-2xl bg-white p-5 shadow-sm";
  const loading = document.createElement("p");
  loading.className = "text-sm text-slate-600";
  loading.textContent = state.loadingText || "Подключаемся к серверу";
  const spinner = document.createElement("span");
  spinner.className = "loading loading-spinner loading-md mt-3 text-lime-500";
  loadingCard.append(loading, spinner);
  root.append(loadingCard);
  return root;
}

function renderAuthScreen() {
  const root = createRoot();
  const panel = document.createElement("section");
  panel.className = "rounded-2xl bg-white p-5 shadow-sm";

  const text = document.createElement("p");
  text.className = state.authErrorMessage ? "text-sm text-rose-600" : "text-sm text-slate-600";
  text.textContent = state.authErrorMessage || "Откройте приложение через Telegram";

  panel.append(text);
  root.append(panel);
  return root;
}

function renderOnboardingScreen() {
  const root = createRoot();

  const form = document.createElement("form");
  form.className = "rounded-2xl bg-white p-5 shadow-sm space-y-4";

  const gender = document.createElement("select");
  gender.className = "select select-bordered w-full rounded-xl border-slate-200 bg-white";
  ["male", "female", "other"].forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    gender.append(option);
  });

  const age = document.createElement("input");
  age.className = "input input-bordered w-full rounded-xl border-slate-200 bg-white";
  age.type = "number";
  age.min = "10";
  age.max = "120";
  age.value = "24";

  const heightCm = document.createElement("input");
  heightCm.className = "input input-bordered w-full rounded-xl border-slate-200 bg-white";
  heightCm.type = "number";
  heightCm.min = "80";
  heightCm.max = "250";
  heightCm.value = "170";

  const weightKg = document.createElement("input");
  weightKg.className = "input input-bordered w-full rounded-xl border-slate-200 bg-white";
  weightKg.type = "number";
  weightKg.min = "20";
  weightKg.max = "400";
  weightKg.step = "0.1";
  weightKg.value = "70";

  const goal = document.createElement("select");
  goal.className = "select select-bordered w-full rounded-xl border-slate-200 bg-white";
  ["lose_weight", "maintain", "gain_weight"].forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    if (value === "lose_weight") option.textContent = "Снижение веса";
    if (value === "maintain") option.textContent = "Поддержание";
    if (value === "gain_weight") option.textContent = "Набор массы";
    goal.append(option);
  });

  form.append(
    createFormField({ label: "Пол", input: gender }),
    createFormField({ label: "Возраст", input: age }),
    createFormField({ label: "Рост (см)", input: heightCm }),
    createFormField({ label: "Вес (кг)", input: weightKg }),
    createFormField({ label: "Цель", input: goal }),
  );

  const submit = createPrimaryButton("Сохранить", () => {
    submitOnboarding({
      gender: gender.value,
      age: Number(age.value),
      heightCm: Number(heightCm.value),
      weightKg: Number(weightKg.value),
      goal: goal.value,
    });
  }, {
    disabled: state.busy,
    loading: state.busy,
  });

  form.append(submit);
  root.append(form);
  return root;
}

function renderMainScreen() {
  const root = createRoot();
  root.append(createDailySummaryCard());

  const uploader = document.createElement("section");
  uploader.className = "rounded-2xl bg-white p-5 shadow-sm";

  const remaining = state.usage?.remaining ?? state.subscription?.remainingToday ?? 0;
  const isLimitExhausted = remaining <= 0;

  const ctaText = document.createElement("p");
  ctaText.className = "mb-3 text-sm text-slate-600";
  ctaText.textContent = isLimitExhausted
    ? "Оформите Premium, чтобы продолжить"
    : "Сфотографируйте блюдо и получите оценку за несколько секунд";

  const descriptionCard = document.createElement("details");
  descriptionCard.className = "mb-4 rounded-2xl bg-slate-50 p-4";
  descriptionCard.open = state.mealDescriptionExpanded;
  descriptionCard.addEventListener("toggle", () => {
    state.mealDescriptionExpanded = descriptionCard.open;
  });

  const descriptionSummary = document.createElement("summary");
  descriptionSummary.className = "cursor-pointer text-sm font-semibold text-slate-800";
  descriptionSummary.textContent = "Описание блюда (необязательно)";

  const helper = document.createElement("p");
  helper.className = "mt-3 text-xs leading-relaxed text-slate-600";
  helper.textContent = "Если блюдо многосоставное... Это повысит точность анализа.";

  const textarea = document.createElement("textarea");
  textarea.className = "mt-3 min-h-24 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 outline-none transition focus:border-lime-400 focus:ring-2 focus:ring-lime-100";
  textarea.placeholder = "Например: гречка + курица...";
  textarea.value = state.mealDescription;
  textarea.maxLength = MEAL_DESCRIPTION_MAX_LENGTH;

  const descriptionFooter = document.createElement("div");
  descriptionFooter.className = "mt-2 flex items-center justify-between gap-2";

  const descriptionError = document.createElement("p");
  descriptionError.className = "text-xs font-medium text-rose-600";

  const counter = document.createElement("p");
  counter.className = "ml-auto text-xs text-slate-500";

  function syncDescriptionMeta() {
    const length = textarea.value.length;
    const error = validateMealDescription(textarea.value) || state.mealDescriptionError;
    counter.textContent = `${length}/${MEAL_DESCRIPTION_MAX_LENGTH}`;

    if (error) {
      textarea.classList.add("border-rose-300", "ring-2", "ring-rose-100");
      textarea.classList.remove("border-slate-200");
      descriptionError.textContent = error;
    } else {
      textarea.classList.remove("border-rose-300", "ring-2", "ring-rose-100");
      textarea.classList.add("border-slate-200");
      descriptionError.textContent = "";
    }
  }

  textarea.addEventListener("input", () => {
    state.mealDescription = textarea.value;
    state.mealDescriptionError = validateMealDescription(state.mealDescription);
    syncDescriptionMeta();
  });

  syncDescriptionMeta();
  descriptionFooter.append(descriptionError, counter);
  descriptionCard.append(descriptionSummary, helper, textarea, descriptionFooter);

  const uploadButton = createPrimaryButton(isLimitExhausted ? "Лимит исчерпан" : "Загрузить фото", () => {
    const currentDescriptionError = validateMealDescription(state.mealDescription);
    if (currentDescriptionError) {
      state.mealDescriptionError = currentDescriptionError;
      state.mealDescriptionExpanded = true;
      render();
      return;
    }
    state.mealDescriptionError = "";
    fileInput.click();
  }, {
    disabled: state.busy || state.analyzing || isLimitExhausted,
    loading: state.analyzing,
    icon: "camera",
  });
  if (isLimitExhausted) {
    uploadButton.classList.add("btn-disabled");
  }

  const fileInput = document.createElement("input");
  fileInput.className = "hidden";
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.capture = "environment";
  fileInput.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    handleAnalyze(file);
    event.target.value = "";
  });

  uploader.append(ctaText, descriptionCard, uploadButton, fileInput);

  if (isLimitExhausted && state.subscription?.status !== "active") {
    const paywallLink = document.createElement("button");
    paywallLink.type = "button";
    paywallLink.className = "mt-3 text-sm font-medium text-violet-700 underline decoration-violet-200 underline-offset-4";
    paywallLink.textContent = "Открыть Premium";
    paywallLink.addEventListener("click", () => {
      state.screen = "paywall";
      render();
    });
    uploader.append(paywallLink);
  }

  root.append(uploader);

  if (state.usage) {
    root.append(createQuotaLabel(state.usage));
  }
  if (state.subscription) {
    root.append(createSubscriptionHint(state.subscription));
  }

  const actions = document.createElement("div");
  actions.className = "grid grid-cols-1 gap-3";
  actions.append(
    createSecondaryButton("История", openHistory, { disabled: state.busy, icon: "history" }),
    createSecondaryButton("Недельный отчет", () => openAnalyticsScreen("weeklyReport"), { disabled: state.busy }),
    createSecondaryButton("Месячный отчет", () => openAnalyticsScreen("monthlyReport"), { disabled: state.busy }),
    createSecondaryButton("Почему вес стоит", () => openAnalyticsScreen("whyNotLosing"), { disabled: state.busy }),
    createSecondaryButton("График веса", () => openAnalyticsScreen("weightChart"), { disabled: state.busy }),
    createSecondaryButton("Подписка", async () => {
      setBusy(true);
      try {
        state.subscription = await getSubscription();
        state.screen = "subscription";
      } catch (error) {
        showToast(mapFriendlyError(error));
      } finally {
        setBusy(false);
        render();
      }
    }, { disabled: state.busy, icon: "crown" }),
    createSecondaryButton("Поделиться прогрессом", openShareScreen, { disabled: state.busy, icon: "share" }),
    createSecondaryButton("Выйти", () => {
      clearSession();
      state.screen = "auth";
      render();
    }, { disabled: state.busy }),
  );

  root.append(actions);
  return root;
}

function renderResultScreen(source = "last") {
  const data = source === "history" ? state.selectedMeal : state.lastAnalyzeResponse?.meal;
  const usage = source === "history" ? null : state.lastAnalyzeResponse?.usage;

  const root = createRoot();

  if (source === "last" && state.analyzing) {
    root.append(createAnalyzeSkeletonScreen());
    return root;
  }

  if (!data?.result) {
    const empty = document.createElement("p");
    empty.className = "text-sm text-slate-600";
    empty.textContent = "Нет данных анализа";
    root.append(empty);
  } else {
    root.append(createResultCard(data.result, {
      mealTime: data.mealTime,
      isPremium: state.subscription?.status === "active",
      hideWarningChip: hasMealDescription(data, source),
      onRetry: source === "last"
        ? () => {
          state.screen = "main";
          render();
        }
        : null,
    }));
  }

  if (usage) {
    root.append(createQuotaLabel(usage));
  }

  return root;
}

function renderHistoryScreen() {
  const root = createRoot();
  root.append(createHistoryList(state.history, openMeal));
  return root;
}

function createAnalyticsLoadingCard() {
  const card = document.createElement("section");
  card.className = "rounded-2xl bg-white p-5 shadow-sm text-sm text-slate-600";
  card.textContent = "Загружаем данные...";
  return card;
}

function renderWeeklyReportScreen() {
  if (!isPremiumActive() || state.premiumGateReason) {
    return createPremiumGateSection(state.premiumGateReason);
  }

  const root = createRoot();
  if (state.analyticsLoading) {
    root.append(createAnalyticsLoadingCard());
    return root;
  }
  if (state.analyticsError) {
    const error = document.createElement("p");
    error.className = "text-sm text-rose-600";
    error.textContent = state.analyticsError;
    root.append(error);
    return root;
  }

  const report = state.weeklyReport || {};
  const totals = report.totals || {};
  const days = Array.isArray(report.days) ? report.days : [];

  const top = document.createElement("section");
  top.className = "rounded-2xl bg-white p-5 shadow-sm";
  const topLabel = document.createElement("p");
  topLabel.className = "text-xs text-slate-500";
  topLabel.textContent = "Недельный итог";
  const topValue = document.createElement("p");
  topValue.className = "mt-2 text-3xl font-semibold text-slate-900";
  topValue.textContent = `${formatMetric(totals.deltaCalories_kcal ?? 0)} ккал`;
  const topHint = document.createElement("p");
  topHint.className = "mt-2 text-sm text-slate-600";
  topHint.textContent = "Баланс за 7 дней";
  top.append(topLabel, topValue, topHint);
  root.append(top);

  const list = document.createElement("section");
  list.className = "rounded-2xl bg-white p-4 shadow-sm";
  for (const day of days) {
    const row = document.createElement("div");
    row.className = "flex items-center justify-between border-b border-slate-100 py-2 last:border-none";

    const date = document.createElement("span");
    date.className = "text-sm text-slate-600";
    date.textContent = formatDayDate(day.date);

    const value = document.createElement("span");
    value.className = "text-sm font-semibold text-slate-900";
    value.textContent = `${formatMetric(day.deltaCalories_kcal ?? 0)} ккал`;

    row.append(date, value);
    list.append(row);
  }
  root.append(list);

  return root;
}

function renderMonthlyReportScreen() {
  if (!isPremiumActive() || state.premiumGateReason) {
    return createPremiumGateSection(state.premiumGateReason);
  }

  const root = createRoot();
  if (state.analyticsLoading) {
    root.append(createAnalyticsLoadingCard());
    return root;
  }
  if (state.analyticsError) {
    const error = document.createElement("p");
    error.className = "text-sm text-rose-600";
    error.textContent = state.analyticsError;
    root.append(error);
    return root;
  }

  const report = state.monthlyReport || {};
  const aggregates = report.aggregates || {};

  const card = document.createElement("section");
  card.className = "rounded-2xl bg-white p-5 shadow-sm";
  const monthLabel = document.createElement("p");
  monthLabel.className = "text-xs text-slate-500";
  monthLabel.textContent = `Месячный отчет ${report.month || ""}`;
  const monthValue = document.createElement("p");
  monthValue.className = "mt-2 text-3xl font-semibold text-slate-900";
  monthValue.textContent = `${formatMetric(aggregates.avgCaloriesPerDay ?? 0, 1)} ккал`;
  const monthHint = document.createElement("p");
  monthHint.className = "mt-2 text-sm text-slate-600";
  monthHint.textContent = "Среднее в день";
  card.append(monthLabel, monthValue, monthHint);

  const grid = document.createElement("div");
  grid.className = "mt-4 grid grid-cols-2 gap-2";
  grid.append(
    createSecondaryInfo("Дефицит", `${formatMetric(aggregates.deficitDays ?? 0)} дн.`),
    createSecondaryInfo("Профицит", `${formatMetric(aggregates.surplusDays ?? 0)} дн.`),
    createSecondaryInfo("Дней с трекингом", `${formatMetric(aggregates.trackedDays ?? 0)}`),
    createSecondaryInfo("Дельта", `${formatMetric(aggregates.deltaCalories_kcal ?? 0)} ккал`),
  );
  card.append(grid);
  root.append(card);
  return root;
}

function renderWhyNotLosingScreen() {
  if (!isPremiumActive() || state.premiumGateReason) {
    return createPremiumGateSection(state.premiumGateReason);
  }

  const root = createRoot();
  if (state.analyticsLoading) {
    root.append(createAnalyticsLoadingCard());
    return root;
  }
  if (state.analyticsError) {
    const error = document.createElement("p");
    error.className = "text-sm text-rose-600";
    error.textContent = state.analyticsError;
    root.append(error);
    return root;
  }

  const data = state.whyNotLosing || {};
  const summary = document.createElement("section");
  summary.className = "rounded-2xl bg-white p-5 shadow-sm";

  const title = document.createElement("p");
  title.className = "text-xs text-slate-500";
  title.textContent = "Почему вес может стоять";

  const text = document.createElement("p");
  text.className = "mt-2 text-sm leading-relaxed text-slate-800";
  text.textContent = data.summary || "Нет данных для анализа.";

  summary.append(title, text);
  root.append(summary);

  const insights = Array.isArray(data.insights) ? data.insights : [];
  if (insights.length) {
    const list = document.createElement("section");
    list.className = "rounded-2xl bg-white p-5 shadow-sm space-y-3";
    for (const insight of insights) {
      const item = document.createElement("article");
      item.className = "rounded-xl border border-slate-100 bg-slate-50 p-3";

      const rule = document.createElement("p");
      rule.className = "text-[11px] font-semibold uppercase tracking-wide text-amber-700";
      rule.textContent = insight.rule || "INSIGHT";

      const body = document.createElement("p");
      body.className = "mt-1 text-sm text-slate-800";
      body.textContent = insight.text || "";

      const rec = document.createElement("p");
      rec.className = "mt-1 text-xs text-slate-600";
      rec.textContent = insight.recommendation || "";

      item.append(rule, body, rec);
      list.append(item);
    }
    root.append(list);
  }

  const goalCard = document.createElement("section");
  goalCard.className = "rounded-2xl bg-white p-5 shadow-sm";

  const goalLabel = document.createElement("p");
  goalLabel.className = "text-sm font-semibold text-slate-900";
  goalLabel.textContent = "Дневная цель калорий";

  const goalHint = document.createElement("p");
  goalHint.className = "mt-1 text-xs text-slate-500";
  goalHint.textContent = "Изменения сохраняются в профиле";

  const goalInput = document.createElement("input");
  goalInput.type = "number";
  goalInput.min = "1000";
  goalInput.max = "5000";
  goalInput.value = state.goalDraft || String(getDailyTarget(state.user?.profile));
  goalInput.className = "input input-bordered mt-3 w-full rounded-xl border-slate-200 bg-white";
  goalInput.addEventListener("input", () => {
    state.goalDraft = goalInput.value;
  });

  const saveBtn = createPrimaryButton("Сохранить цель", saveGoalFromWhyNotLosing, {
    loading: state.goalSaving,
    disabled: state.goalSaving,
  });
  saveBtn.classList.add("mt-3");

  goalCard.append(goalLabel, goalHint, goalInput, saveBtn);
  root.append(goalCard);

  return root;
}

function renderWeightChartScreen() {
  if (!isPremiumActive() || state.premiumGateReason) {
    return createPremiumGateSection(state.premiumGateReason);
  }

  const root = createRoot();
  if (state.analyticsLoading) {
    root.append(createAnalyticsLoadingCard());
    return root;
  }
  if (state.analyticsError) {
    const error = document.createElement("p");
    error.className = "text-sm text-rose-600";
    error.textContent = state.analyticsError;
    root.append(error);
    return root;
  }

  const payload = state.weightChart || {};
  const items = Array.isArray(payload.items) ? payload.items : [];
  const card = document.createElement("section");
  card.className = "weight-chart-card";

  const heading = document.createElement("p");
  heading.className = "text-xs text-slate-500";
  heading.textContent = "Динамика веса";
  card.append(heading);

  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "mt-3 text-sm text-slate-600";
    empty.textContent = "Пока нет данных веса.";
    card.append(empty);
    root.append(card);
    return root;
  }

  const width = 320;
  const height = 180;
  const padding = 20;
  const path = buildChartPath(items, width, height, padding);

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "weight-chart-svg");

  const poly = document.createElementNS("http://www.w3.org/2000/svg", "path");
  poly.setAttribute("d", path);
  poly.setAttribute("class", "weight-chart-line");

  svg.append(poly);
  card.append(svg);

  const foot = document.createElement("p");
  foot.className = "mt-3 text-xs text-slate-500";
  const first = items[0];
  const last = items[items.length - 1];
  foot.textContent = `${formatDayDate(first?.date)}: ${formatMetric(first?.weight, 1)} кг -> ${formatDayDate(last?.date)}: ${formatMetric(last?.weight, 1)} кг`;
  card.append(foot);

  root.append(card);
  return root;
}

function renderPaywallScreen() {
  if (state.subscription?.status === "active") {
    state.screen = "main";
    return renderMainScreen();
  }

  const root = createRoot();

  const card = document.createElement("section");
  card.className = "rounded-2xl bg-white p-5 shadow-md";

  const offerTag = document.createElement("p");
  offerTag.className = "inline-flex w-fit rounded-full bg-violet-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-violet-700";
  offerTag.textContent = "Premium";

  const offerLine = document.createElement("p");
  offerLine.className = "mt-3 text-lg font-semibold text-slate-900";
  offerLine.textContent = "Откройте Premium и снимите лимиты";

  const priceWrap = document.createElement("div");
  priceWrap.className = "mt-3 flex items-end gap-2";

  const oldPrice = document.createElement("span");
  oldPrice.className = "text-lg font-semibold text-slate-400 line-through";
  oldPrice.textContent = "1499 ₽";

  const currentPrice = document.createElement("span");
  currentPrice.className = "text-4xl font-extrabold tracking-tight text-emerald-600";
  currentPrice.textContent = "499 ₽";

  const period = document.createElement("span");
  period.className = "pb-1 text-sm font-medium text-slate-500";
  period.textContent = "/ 30 дней";

  priceWrap.append(oldPrice, currentPrice, period);

  const benefits = document.createElement("ul");
  benefits.className = "mt-5 space-y-2";
  [
    "До 20 фото в день вместо 2",
    "Более точное распознавание продуктов",
    "Быстрая оценка калорий и БЖУ",
    "Удобнее вести питание каждый день",
    "История анализов всегда под рукой",
  ].forEach((line) => {
    const item = document.createElement("li");
    item.className = "flex items-start gap-2 text-sm text-slate-700";

    const marker = document.createElement("span");
    marker.className = "mt-1 inline-flex h-2 w-2 shrink-0 rounded-full bg-violet-300";

    const text = document.createElement("span");
    text.textContent = line;

    item.append(marker, text);
    benefits.append(item);
  });

  const footnote = document.createElement("p");
  footnote.className = "mt-4 text-xs text-slate-500";
  footnote.textContent = "Лимит Premium: 20 фото в день.";

  card.append(offerTag, offerLine, priceWrap, benefits, footnote);

  const paidBanner = document.createElement("div");
  paidBanner.className = "mt-4 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs text-emerald-700";
  paidBanner.textContent = "Уже оплатили? Нажмите кнопку ниже, чтобы обновить статус подписки.";
  card.append(paidBanner);
  root.append(card);

  const upgradeButton = createPrimaryButton("Получить Premium за 499 ₽", handleUpgrade, {
    disabled: state.busy,
    loading: state.busy,
    icon: "crown",
  });
  upgradeButton.classList.add("h-14", "text-base", "font-bold", "shadow-sm");

  root.append(
    upgradeButton,
    createSecondaryButton("Я уже оплатил(а)", async () => {
      if (state.busy) {
        return;
      }
      setBusy(true);
      await refreshAfterResume({ notifyResult: true });
      state.screen = state.subscription?.status === "active" ? "main" : "paywall";
      setBusy(false);
      render();
    }, { disabled: state.busy, icon: "refresh" }),
  );

  return root;
}

function renderSubscriptionScreen() {
  const root = createRoot();
  ensureReminderState();

  const sub = state.subscription || {};
  const card = document.createElement("section");
  card.className = "rounded-2xl bg-white p-5 shadow-sm";

  const badge = document.createElement("div");
  const isActive = sub.status === "active";
  badge.className = isActive
    ? "mb-4 inline-flex w-fit rounded-full bg-violet-100 px-3 py-1 text-xs font-medium text-violet-700"
    : "mb-4 inline-flex w-fit rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600";
  badge.textContent = isActive ? "Premium активен" : `Статус: ${sub.status || "free"}`;

  const grid = document.createElement("div");
  grid.className = "grid grid-cols-2 gap-2";
  grid.append(
    createSecondaryInfo("Активна до", sub.activeUntil ? new Date(sub.activeUntil).toLocaleString("ru-RU") : "-"),
    createSecondaryInfo("Стоимость", "499 ₽ / 30 дней"),
    createSecondaryInfo("Лимит", `${sub.dailyLimit ?? 0}`),
    createSecondaryInfo("Использовано", `${sub.usedToday ?? 0}`),
    createSecondaryInfo("Осталось", `${sub.remainingToday ?? 0}`),
  );

  card.append(badge, grid);

  root.append(card);
  if (!isPremiumActive()) {
    root.append(createPremiumGateCard({
      onCta: handleUpgrade,
      note: "Редактирование напоминаний доступно только в Premium.",
    }));
  } else {
    const reminders = document.createElement("section");
    reminders.className = "rounded-2xl bg-white p-5 shadow-sm";

    const remindersTitle = document.createElement("p");
    remindersTitle.className = "text-sm font-semibold text-slate-900";
    remindersTitle.textContent = "Уведомления";

    const remindersRow = document.createElement("label");
    remindersRow.className = "mt-3 flex items-start justify-between gap-3";

    const remindersText = document.createElement("div");

    const remindersLabel = document.createElement("p");
    remindersLabel.className = "text-sm text-slate-800";
    remindersLabel.textContent = "Напоминать о прогрессе";

    const remindersHelper = document.createElement("p");
    remindersHelper.className = "mt-1 text-xs text-slate-500";
    remindersHelper.textContent = "Раз в день вечером, если прогресс низкий";

    remindersText.append(remindersLabel, remindersHelper);

    const remindersToggle = document.createElement("input");
    remindersToggle.type = "checkbox";
    remindersToggle.className = "toggle toggle-success mt-0.5";
    remindersToggle.checked = Boolean(state.reminderEnabled);
    remindersToggle.disabled = state.reminderSaving || state.busy;
    remindersToggle.addEventListener("change", (event) => {
      handleReminderToggle(event.target.checked);
    });

    remindersRow.append(remindersText, remindersToggle);
    reminders.append(remindersTitle, remindersRow);

    const toneGroup = document.createElement("fieldset");
    toneGroup.className = "mt-4 rounded-xl border border-slate-200 bg-slate-50 p-3";

    const toneOptions = [
      { value: "soft", label: "Мягкий стиль" },
      { value: "balanced", label: "Баланс" },
      { value: "hard", label: "Жесткий стиль" },
    ];
    const selectedTone = normalizeReminderTone(state.reminderTone) || REMINDER_TONE_DEFAULT;

    for (const option of toneOptions) {
      const toneRow = document.createElement("label");
      toneRow.className = "flex items-center gap-2 py-1.5 text-sm text-slate-800";

      const toneInput = document.createElement("input");
      toneInput.type = "radio";
      toneInput.name = "notification-tone";
      toneInput.value = option.value;
      toneInput.className = "radio radio-sm radio-success";
      toneInput.checked = selectedTone === option.value;
      toneInput.disabled = state.reminderSaving || state.busy;
      toneInput.addEventListener("change", () => {
        if (toneInput.checked) {
          handleReminderToneChange(option.value);
        }
      });

      const toneLabel = document.createElement("span");
      toneLabel.textContent = option.label;

      toneRow.append(toneInput, toneLabel);
      toneGroup.append(toneRow);
    }

    const toneHelper = document.createElement("p");
    toneHelper.className = "mt-2 text-xs text-slate-500";
    toneHelper.textContent = "Определяет тон сообщений в Telegram";

    reminders.append(toneGroup, toneHelper);

    if (state.reminderSaving) {
      const savingLabel = document.createElement("p");
      savingLabel.className = "mt-3 text-xs text-slate-500";
      savingLabel.textContent = "Сохраняем...";
      reminders.append(savingLabel);
    }

    root.append(reminders);
  }
  root.append(createSecondaryButton("Обновить статус", async () => {
    setBusy(true);
    try {
      state.subscription = await getSubscription();
      await refreshUsageAndSubscription();
    } catch (error) {
      showToast(mapFriendlyError(error));
    } finally {
      setBusy(false);
      render();
    }
  }, { disabled: state.busy, loading: state.busy, icon: "refresh" }));
  return root;
}

async function openShareScreen(options = {}) {
  const { pushRoute = true } = options;
  if (!pushRoute) {
    suppressNextHistoryPush = true;
  }
  setBusy(true);
  try {
    const today = getTodayUtcDate();
    const [profile, streak, dailyStats] = await Promise.all([
      getMe().catch(() => state.user),
      getStreak().catch(() => ({ currentStreak: 0, bestStreak: 0, lastCompletedDate: null })),
      getStatsDaily(today).catch(() => ({ calories_kcal: 0, protein_g: 0, fat_g: 0, carbs_g: 0, mealsCount: 0 })),
    ]);

    if (profile) {
      state.user = profile;
      syncReminderStateFrom(state.user);
    }

    const dailyGoal = getDailyTarget(profile?.profile || state.user?.profile) || 2000;

    state.shareData = {
      currentStreak: streak?.currentStreak ?? 0,
      todayCalories: dailyStats?.calories_kcal ?? 0,
      dailyGoal,
      motivationalQuote: pickRandomQuote(QUOTES_SHARE_SHORT).text,
    };
    state.screen = "share";
  } catch (error) {
    showToast(mapFriendlyError(error));
  } finally {
    setBusy(false);
    render();
  }
}

function renderShareScreen() {
  const shareData = state.shareData || {
    currentStreak: state.streak?.currentStreak ?? 0,
    todayCalories: state.dailyStats?.calories_kcal ?? 0,
    dailyGoal: getDailyTarget(state.user?.profile) || 2000,
    motivationalQuote: pickRandomQuote(QUOTES_SHARE_SHORT).text,
  };

  const shareScreen = createShareCard({
    currentStreak: shareData.currentStreak,
    todayCalories: shareData.todayCalories,
    dailyGoal: shareData.dailyGoal,
    motivationalQuote: shareData.motivationalQuote,
  });

  const container = document.createElement("div");
  container.append(shareScreen);

  return container;
}

function ensureToastMount() {
  if (document.getElementById("toast")) {
    return;
  }
  const toast = document.createElement("div");
  toast.id = "toast";
  toast.className = "alert toast-base toast-hidden alert-error";
  toast.hidden = true;
  document.body.append(toast);
}

function restoreScroll(screen) {
  const y = state.scrollByScreen[screen] ?? 0;
  window.requestAnimationFrame(() => {
    window.scrollTo(0, y);
  });
}

function createScreenLoader() {
  const loader = document.createElement("div");
  loader.id = "app-screen-loader";
  loader.className = "fixed right-4 top-4 z-40 rounded-full border border-slate-200 bg-white/90 p-2 shadow-sm";
  const spinner = document.createElement("span");
  spinner.className = "loading loading-spinner loading-sm text-lime-500";
  loader.append(spinner);
  return loader;
}

function createSecondaryInfo(label, value) {
  const box = document.createElement("div");
  box.className = "rounded-xl border border-slate-100 bg-white p-3";

  const l = document.createElement("div");
  l.className = "text-xs text-slate-500";
  l.textContent = label;

  const v = document.createElement("div");
  v.className = "mt-1 text-sm font-medium text-slate-700";
  v.textContent = value;

  box.append(l, v);
  return box;
}

function hasBrowserHistory() {
  return window.history.length > 1;
}

function goBackFromHeader() {
  if (hasBrowserHistory()) {
    window.history.back();
    return;
  }
  if (state.screen !== "main") {
    state.screen = "main";
    render();
  }
}

function createMainHeaderActions() {
  const actions = document.createElement("div");
  actions.className = "flex items-center gap-2";

  if (state.streak) {
    actions.append(createStreakBadge(state.streak, openStreakModal));
  }
  actions.append(createPremiumHeaderButton());
  return actions;
}

function getHeaderMeta(screen) {
  switch (screen) {
    case "auth":
      return { title: "Вход", subtitle: "Авторизация через Telegram WebApp" };
    case "onboarding":
      return { title: "Анкета", subtitle: "Нужно заполнить один раз" };
    case "result":
      return { title: "Результат", subtitle: "Оценка по фото" };
    case "history":
      return { title: "История", subtitle: "Последние приемы пищи" };
    case "historyDetail":
      return { title: "Прием пищи", subtitle: "Детали анализа" };
    case "paywall":
      return { title: "Открыть Premium", subtitle: "Больше фото, меньше ограничений" };
    case "subscription":
      return { title: "Подписка", subtitle: "Текущий статус" };
    case "weeklyReport":
      return { title: "Недельный отчет", subtitle: "Расширенная аналитика" };
    case "monthlyReport":
      return { title: "Месячный отчет", subtitle: "Сводка за месяц" };
    case "whyNotLosing":
      return { title: "Почему вес стоит", subtitle: "Причины и рекомендации" };
    case "weightChart":
      return { title: "График веса", subtitle: "Тренд по дням" };
    case "share":
      return { title: "Поделиться", subtitle: "Карточка прогресса" };
    case "loading":
      return { title: "FitAI", subtitle: "Загрузка..." };
    case "main":
    default:
      return { title: "FitAI", subtitle: "Фото -> калории за пару секунд" };
  }
}

function renderHeader(screen) {
  if (!appHeader) {
    return;
  }

  const { title, subtitle } = getHeaderMeta(screen);
  const isMain = screen === "main";
  const left = isMain ? null : createHeaderBackButton(goBackFromHeader);
  const right = isMain ? createMainHeaderActions() : null;

  appHeader.innerHTML = "";
  appHeader.append(createHeaderShell({ title, subtitle, left, right }));
}

function render() {
  if (!app || !appHeader || !appContent) {
    return;
  }
  ensureToastMount();
  const previousScreen = state.currentScreen || state.screen;
  state.scrollByScreen[previousScreen] = window.scrollY;
  appContent.innerHTML = "";
  document.getElementById("app-screen-loader")?.remove();
  document.querySelector(".streak-modal-overlay")?.remove();

  renderHeader(state.screen);

  let screenNode;
  switch (state.screen) {
    case "auth":
      screenNode = renderAuthScreen();
      break;
    case "onboarding":
      screenNode = renderOnboardingScreen();
      break;
    case "main":
      screenNode = renderMainScreen();
      break;
    case "result":
      screenNode = renderResultScreen("last");
      break;
    case "history":
      screenNode = renderHistoryScreen();
      break;
    case "historyDetail":
      screenNode = renderResultScreen("history");
      break;
    case "paywall":
      screenNode = renderPaywallScreen();
      break;
    case "subscription":
      screenNode = renderSubscriptionScreen();
      break;
    case "weeklyReport":
      screenNode = renderWeeklyReportScreen();
      break;
    case "monthlyReport":
      screenNode = renderMonthlyReportScreen();
      break;
    case "whyNotLosing":
      screenNode = renderWhyNotLosingScreen();
      break;
    case "weightChart":
      screenNode = renderWeightChartScreen();
      break;
    case "share":
      screenNode = renderShareScreen();
      break;
    case "loading":
    default:
      screenNode = renderLoadingScreen();
      break;
  }

  const screenContainer = document.createElement("div");
  const shouldAnimateScreen = previousScreen !== state.screen;
  screenContainer.className = shouldAnimateScreen ? "screen screen-animate" : "screen screen-enter";
  screenContainer.append(screenNode);
  appContent.append(screenContainer);

  if (shouldAnimateScreen) {
    window.requestAnimationFrame(() => {
      screenContainer.classList.add("screen-enter");
    });
  }

  if (state.busy) {
    app.append(createScreenLoader());
  }

  if (state.streakModalOpen && state.streak) {
    const modal = createStreakModal(state.streak, closeStreakModal);
    app.append(modal);
    // Focus the modal for accessibility
    window.requestAnimationFrame(() => {
      modal.focus();
    });
  }

  if (previousScreen !== state.screen) {
    restoreScroll(state.screen);
  }
  syncBrowserHistory(previousScreen);
  suppressNextHistoryPush = false;
  syncTelegramBackButton();
  state.currentScreen = state.screen;
  maybeShowQuoteOverlay();
}

window.addEventListener("popstate", (event) => {
  const historyScreen = event.state?.screen;
  if (historyScreen && historyScreen !== state.screen) {
    suppressNextHistoryPush = true;
    state.screen = historyScreen;
    render();
    return;
  }

  if (isSharePath()) {
    if (state.token && state.user?.isOnboarded) {
      openShareScreen({ pushRoute: false });
    }
    return;
  }

  if (state.screen === "share") {
    suppressNextHistoryPush = true;
    leaveShareScreen();
  }
});

window.addEventListener("focus", refreshAfterResume);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshAfterResume();
  }
});

render();
bootstrapAuth();
