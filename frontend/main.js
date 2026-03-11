import "./styles.css";
import {
  ApiError,
  authTelegram,
  createYookassaPayment,
  refreshYookassaPayment,
  getMe,
  getMealById,
  getMeals,
  getSubscription,
  getWeightChart,
  patchNotificationSettings,
  getStatsDaily,
  getStatsWeekly,
  getUsageToday,
  logWeight,
  getStreak,
  analyzeMeal,
  analyzeMealStep1,
  analyzeMealStep2,
  setTokenGetter,
  setSilentReauthHandler,
  setTokenInvalidator,
  setUnauthorizedHandler,
  updateProfile,
  updateProfileGoal,
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
const STORAGE_TG_INITDATA_KEY = "fitai.telegramInitData";
const MEAL_DESCRIPTION_MAX_LENGTH = 500;
const APP_ENV = import.meta.env.VITE_APP_ENV ?? import.meta.env.MODE;
const IS_DEV = APP_ENV !== "production";
const TOKEN_REFRESH_EARLY_MS = 5 * 60 * 1000;
const AUTH_RETRY_COOLDOWN_MS = 30 * 1000;
const REMINDER_TONE_DEFAULT = "balanced";
const PREMIUM_ANALYTICS_SCREENS = new Set([
  "weightChart",
]);

const state = {
  token: localStorage.getItem(STORAGE_TOKEN_KEY),
  user: null,
  usage: null,
  subscription: null,
  dailyStats: null,
  weeklyStats: null,
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
  weightChart: null,
  analyticsLoading: false,
  analyticsError: "",
  premiumGateReason: "",
  analysisStep1: null,
  analysisDraftItems: [],
  analysisStep2Result: null,
  analysisFeedback: "",
  weightEntryOpen: false,
  weightEntryValue: "",
  weightEntrySaving: false,
  goalEntryOpen: false,
  goalEntryValue: "",
  goalEntrySaving: false,
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
  if (typeof tgWebApp.isVersionAtLeast === "function" && !tgWebApp.isVersionAtLeast("6.1")) {
    return;
  }
  tgWebApp.BackButton.offClick(leaveShareScreen);
  tgWebApp.BackButton.hide();
}

function extractAuthDateFromInitData(initData) {
  const raw = String(initData || "").trim();
  if (!raw) {
    return null;
  }

  const params = new URLSearchParams(raw);
  const authDateRaw = params.get("auth_date");
  const authDate = Number(authDateRaw);
  if (!Number.isFinite(authDate) || authDate <= 0) {
    return null;
  }
  return authDate;
}

function isInitDataFresh(initData, maxAgeSeconds = 60 * 60 * 24) {
  const authDate = extractAuthDateFromInitData(initData);
  if (!authDate) {
    return false;
  }
  const nowSeconds = Math.floor(Date.now() / 1000);
  return nowSeconds - authDate <= maxAgeSeconds;
}

function getTelegramInitData(options = {}) {
  const { allowCached = true } = options;
  const fromWebApp = tgWebApp?.initData || window.Telegram?.WebApp?.initData || "";
  if (fromWebApp) {
    try {
      sessionStorage.setItem(STORAGE_TG_INITDATA_KEY, fromWebApp);
    } catch (_error) {
      // ignore storage errors
    }
    return fromWebApp;
  }

  const readParam = (rawValue) => String(rawValue || "").trim();

  const searchParams = new URLSearchParams(window.location.search || "");
  const fromSearch = readParam(searchParams.get("tgWebAppData") || searchParams.get("initData"));
  if (fromSearch) {
    try {
      sessionStorage.setItem(STORAGE_TG_INITDATA_KEY, fromSearch);
    } catch (_error) {
      // ignore storage errors
    }
    return fromSearch;
  }

  const hashRaw = window.location.hash?.startsWith("#")
    ? window.location.hash.slice(1)
    : (window.location.hash || "");
  const hashParams = new URLSearchParams(hashRaw);
  const fromHash = readParam(hashParams.get("tgWebAppData") || hashParams.get("initData"));
  if (fromHash) {
    try {
      sessionStorage.setItem(STORAGE_TG_INITDATA_KEY, fromHash);
    } catch (_error) {
      // ignore storage errors
    }
    return fromHash;
  }

  if (!allowCached) {
    return "";
  }

  try {
    const cached = sessionStorage.getItem(STORAGE_TG_INITDATA_KEY) || "";
    if (cached && isInitDataFresh(cached, 60 * 10)) {
      return cached;
    }
    return "";
  } catch (_error) {
    return "";
  }
}

async function waitForTelegramInitData(maxWaitMs = 2000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < maxWaitMs) {
    const initData = getTelegramInitData({ allowCached: false });
    if (initData) {
      return initData;
    }
    await new Promise((resolve) => {
      window.setTimeout(resolve, 120);
    });
  }
  return getTelegramInitData({ allowCached: true });
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

function confidenceLabelRu(value) {
  const v = Number(value || 0);
  if (v > 0.8) return "высокая уверенность";
  if (v >= 0.5) return "средняя";
  return "низкая — уточните";
}

function routeByBusinessError(error) {
  if (!(error instanceof ApiError)) {
    return false;
  }
  if (error.code === "UNAUTHORIZED") {
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

function getProfileWeightKg() {
  const raw = state.user?.profile?.weightKg;
  const parsed = parseFloat(String(raw ?? "").replace(",", "."));
  if (Number.isFinite(parsed) && parsed >= 20 && parsed <= 400) {
    return parsed;
  }
  return null;
}


function handleAddWeight() {
  if (state.busy) {
    return;
  }
  const currentWeight = getProfileWeightKg() || 70;
  state.weightEntryValue = String(currentWeight);
  state.weightEntryOpen = true;
  render();
}

function closeWeightEntry() {
  if (state.weightEntrySaving) {
    return;
  }
  state.weightEntryOpen = false;
  render();
}

async function submitWeightEntry() {
  const parsed = parseFloat(String(state.weightEntryValue || "").replace(",", "."));
  if (!Number.isFinite(parsed) || parsed < 20 || parsed > 400) {
    showToast("Пожалуйста, введите корректный вес (от 20 до 400 кг)");
    return;
  }

  if (state.weightEntrySaving) {
    return;
  }
  state.weightEntrySaving = true;

  const withTimeout = (promise, timeoutMs = 15000) => Promise.race([
    promise,
    new Promise((_, reject) => {
      window.setTimeout(() => reject(new Error("timeout")), timeoutMs);
    }),
  ]);

  // Optimistic UI update: close modal immediately and update local graph.
  if (!state.user.profile) {
    state.user.profile = {};
  }
  state.user.profile.weightKg = parsed;
  if (!Array.isArray(state.weightChart?.items)) {
    state.weightChart = { items: [] };
  }
  const today = formatDateForApi(new Date());
  const existingIndex = state.weightChart.items.findIndex((item) => String(item?.date) === today);
  if (existingIndex >= 0) {
    state.weightChart.items[existingIndex] = { date: today, weight: parsed };
  } else {
    state.weightChart.items.push({ date: today, weight: parsed });
    state.weightChart.items.sort((a, b) => String(a.date).localeCompare(String(b.date)));
  }
  state.weightEntryOpen = false;
  render();

  try {
    await withTimeout(logWeight(formatDateForApi(new Date()), parsed));
    showToast("Вес сохранен!", "info");
    refreshUsageAndSubscription().then(render).catch(() => {});
  } catch (err) {
    if (err instanceof Error && err.message === "timeout") {
      showToast("Сохранение веса заняло слишком много времени");
    } else {
      showToast(mapFriendlyError(err));
    }
  } finally {
    state.weightEntrySaving = false;
  }
}

function getDailyTarget(profile) {
  if (profile?.dailyGoal) return profile.dailyGoal;
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

function formatDateForApi(date) {
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) {
    return getTodayUtcDate();
  }
  return d.toISOString().slice(0, 10);
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
  button.className = "btn-premium-header";
  const isActive = state.subscription?.status === "active";
  button.textContent = isActive ? "✦ Premium" : "Premium";
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
  card.className = "glass daily-card fade-up d1";

  const consumed = Math.round(state.dailyStats?.calories_kcal ?? 0);
  const target = getDailyTarget(state.user?.profile);
  const remaining = Math.max(0, target - consumed);
  const progress = clampPercent((consumed / Math.max(1, target)) * 100);

  const topLabel = document.createElement("p");
  topLabel.className = "daily-card-label";
  topLabel.textContent = "Сегодня";

  const kcalEl = document.createElement("p");
  kcalEl.className = "daily-card-kcal";
  const kcalNum = document.createTextNode(consumed.toLocaleString("ru-RU"));
  const kcalSup = document.createElement("sup");
  kcalSup.textContent = "ккал";
  kcalEl.append(kcalNum, kcalSup);

  const subMetaRow = document.createElement("div");
  subMetaRow.className = "daily-card-meta-row";

  const subLabel = document.createElement("p");
  subLabel.className = "daily-card-sub";
  subLabel.textContent = `из ${target.toLocaleString("ru-RU")} целевых · осталось ${remaining.toLocaleString("ru-RU")}`;

  const editGoalBtn = document.createElement("button");
  editGoalBtn.type = "button";
  editGoalBtn.className = "inline-icon-btn";
  editGoalBtn.title = "Изменить цель";
  editGoalBtn.setAttribute("aria-label", "Изменить цель по калориям");
  editGoalBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:0.875rem;height:0.875rem;"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>`;
  editGoalBtn.addEventListener("click", () => {
    if (state.goalEntrySaving) {
      return;
    }
    state.goalEntryValue = String(target);
    state.goalEntryOpen = true;
    render();
  });
  
  subMetaRow.append(subLabel, editGoalBtn);

  // Row: ring + macros
  const row = document.createElement("div");
  row.className = "daily-card-row";

  // Ring
  const ringWrap = document.createElement("div");
  ringWrap.className = "daily-ring-wrap";

  const circumference = 2 * Math.PI * 36;
  const offset = circumference * (1 - progress / 100);
  ringWrap.innerHTML = `
    <svg viewBox="0 0 88 88" width="88" height="88" style="transform:rotate(-90deg)">
      <circle class="ring-track" cx="44" cy="44" r="36"/>
      <circle class="ring-fill" cx="44" cy="44" r="36"
        stroke-dasharray="${circumference.toFixed(2)}"
        stroke-dashoffset="${offset.toFixed(2)}"/>
    </svg>
    <div class="ring-center">
      <span class="ring-pct">${Math.round(progress)}%</span>
      <span class="ring-pct-label">цель</span>
    </div>`;

  // Macros
  const macros = document.createElement("div");
  macros.className = "daily-macros";

  
  const protein = Math.round(state.dailyStats?.protein_g ?? 0);
  const fat = Math.round(state.dailyStats?.fat_g ?? 0);
  const carbs = Math.round(state.dailyStats?.carbs_g ?? 0);
  
  // Dynamic macro recalculation based on daily goal
  // Assuming standard ratio: 30% protein, 30% fat, 40% carbs
  const proteinTarget = Math.round((target * 0.3) / 4);
  const fatTarget = Math.round((target * 0.3) / 9);
  const carbsTarget = Math.round((target * 0.4) / 4);


  for (const [label, val, tgt, color] of [
    ["Белки", protein, proteinTarget, "var(--sage)"],
    ["Жиры", fat, fatTarget, "var(--clay)"],
    ["Углев", carbs, carbsTarget, "var(--oat)"],
  ]) {
    const macroRow = document.createElement("div");
    macroRow.className = "macro-row";

    const lbl = document.createElement("span");
    lbl.className = "macro-label";
    lbl.textContent = label;

    const barWrap = document.createElement("div");
    barWrap.className = "macro-bar-wrap";
    const bar = document.createElement("div");
    bar.className = "macro-bar";
    bar.style.width = `${clampPercent((val / Math.max(1, tgt)) * 100)}%`;
    bar.style.background = color;
    barWrap.append(bar);

    const valEl = document.createElement("span");
    valEl.className = "macro-val";
    valEl.textContent = `${val}г`;

    macroRow.append(lbl, barWrap, valEl);
    macros.append(macroRow);
  }

  row.append(ringWrap, macros);

  const track = createProgressBar({ value: progress, max: 100, label: "Прогресс" });
  track.style.display = "none"; // hidden, we use ring instead

  card.append(topLabel, kcalEl, subMetaRow, row);

  if (state.subscription?.status === "active" && state.subscription?.activeUntil) {
    const premium = document.createElement("div");
    premium.className = "daily-premium-badge";
    premium.textContent = `✦ Премиум активен до ${formatDate(state.subscription.activeUntil)}`;
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
  notifyTelegramAppReady();
  const initData = await waitForTelegramInitData();
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
  try {
    const response = await authTelegram(initData);
    saveToken(response.accessToken);
    nextAutoAuthAttemptAt = 0;
    notifyTelegramAppReady();
    return await bootstrapUser();
  } catch (error) {
    const isInitDataError = error instanceof ApiError
      && (error.code === "AUTH_INVALID_INITDATA" || error.code === "AUTH_EXPIRED_INITDATA");
    if (
      isInitDataError
    ) {
      try {
        sessionStorage.removeItem(STORAGE_TG_INITDATA_KEY);
      } catch (_removeError) {
        // ignore storage errors
      }
      clearSession();
      nextAutoAuthAttemptAt = Date.now() + AUTH_RETRY_COOLDOWN_MS;
      state.authErrorMessage = "Ошибка авторизации. Откройте приложение через Telegram.";
      state.screen = "auth";
      render();
      return false;
    }

    if (state.token) {
      const loadedWithToken = await bootstrapUser();
      if (loadedWithToken) {
        return true;
      }
    }

    if (error instanceof ApiError && (error.code === "NETWORK" || error.code === "NETWORK_ERROR")) {
      state.authErrorMessage = "Нет соединения с сервером. Попробуйте снова.";
    } else {
      state.authErrorMessage = "Не удалось загрузить данные. Попробуйте снова.";
    }
    clearSession();
    nextAutoAuthAttemptAt = Date.now() + AUTH_RETRY_COOLDOWN_MS;
    state.screen = "auth";
    render();
    return false;
  } finally {
    state.loadingText = "Подключаемся к серверу";
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
    return false;
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
    return true;
  } catch (error) {
    if (!routeByBusinessError(error)) {
      showToast("Не удалось загрузить данные профиля");
      state.authErrorMessage = "Не удалось загрузить данные. Попробуйте снова.";
      state.screen = "auth";
    }
    return false;
  } finally {
    setBusy(false);
    render();
  }
}

async function refreshUsageAndSubscription() {
  if (!state.token) {
    return;
  }
  const [usage, subscription, dailyStats, weeklyStats, weightChart, streak] = await Promise.all([
    getUsageToday(),
    getSubscription(),
    getStatsDaily(getTodayUtcDate()),
    getStatsWeekly().catch(() => ({ days: [] })),
    getWeightChart().catch(() => ({ items: [] })),
    getStreak().catch(() => ({ currentStreak: 0, bestStreak: 0, lastCompletedDate: null })),
  ]);
  state.usage = usage;
  state.subscription = subscription;
  state.dailyStats = dailyStats;
  state.weeklyStats = weeklyStats;
  state.weightChart = weightChart;
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
    const step1 = await analyzeMealStep1(file, {
      description: mealDescription,
    });

    state.analysisStep1 = step1;
    state.analysisDraftItems = (step1.items || []).map((item) => {
      const defaultWeight = Number(item.defaultWeightG);
      const safeWeight = Number.isFinite(defaultWeight) && defaultWeight > 0 ? defaultWeight : 150;
      return {
        clientItemId: item.clientItemId,
        name: item.name,
        editedName: item.name,
        matchType: item.matchType,
        confidence: Number(item.confidence || 0),
        warnings: Array.isArray(item.warnings) ? item.warnings : [],
        kbjuSource: item.matchType === "exact" ? "exact" : item.matchType === "fuzzy" ? "fallback" : "unknown",
        weightG: Math.max(1, Math.round(safeWeight)),
      };
    });

    if (state.analysisDraftItems.length > 0) {
      state.screen = "analysisAdjust";
    } else {
      const response = await analyzeMeal(file, {
        description: mealDescription,
      });
      state.lastAnalyzeResponse = response;
      state.usage = response.usage;
      state.screen = "result";
    }

    state.mealDescription = "";

    // Optimistic daily summary update so the main screen reflects calories immediately.
    const prev = state.dailyStats || {};
    const totals = state.lastAnalyzeResponse?.meal?.result?.totals || {};
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

    if (state.screen !== "analysisAdjust") {
      state.screen = "result";
    }
  } catch (error) {
    if (!routeByBusinessError(error)) {
      state.screen = previousScreen;
      if (isMissingImageValidationError(error)) {
        showToast("Добавьте фото");
      } else {
        showToast(mapAnalyzeErrorToToast(error));
      }
    }
  } finally {
    state.analyzing = false;
    setBusy(false);
    render();
  }
}

async function submitAnalysisStep2() {
  if (!state.analysisStep1?.analysisSessionId) {
    showToast("Сессия анализа не найдена");
    return;
  }

  setBusy(true);
  try {
    const payload = {
      analysisSessionId: state.analysisStep1.analysisSessionId,
      mealTime: "unknown",
      items: state.analysisDraftItems.map((item) => ({
        clientItemId: item.clientItemId,
        weight_g: Math.max(1, Number(item.weightG || 1)),
        adjustedName: String(item.editedName || item.name || "").trim() || undefined,
      })),
    };
    const response = await analyzeMealStep2(payload);
    state.analysisStep2Result = response;
    state.lastAnalyzeResponse = response;
    state.usage = response.usage;
    state.screen = "analysisSummary";
  } catch (error) {
    if (error instanceof ApiError && error.code === "NOT_FOUND") {
      showToast("Сессия истекла. Загрузите фото заново.");
      state.screen = "main";
    } else {
      showToast(mapFriendlyError(error));
    }
  } finally {
    setBusy(false);
    render();
  }
}

function renderAnalysisAdjustScreen() {
  const root = createRoot();
  const step1 = state.analysisStep1;
  const items = state.analysisDraftItems || [];

  const header = document.createElement("section");
  header.className = "glass analytics-card fade-up d1";

  const title = document.createElement("p");
  title.className = "analytics-label";
  title.textContent = "Шаг 2 — Проверьте вес";

  const subtitle = document.createElement("p");
  subtitle.style.cssText = "font-size:0.9375rem;font-weight:500;color:var(--bark);margin-top:0.25rem;";
  subtitle.textContent = "Уточните граммовку каждого блюда";

  const expiry = document.createElement("p");
  expiry.className = "analytics-hint";
  expiry.textContent = `Сессия активна до ${new Date(step1?.expiresAt || Date.now()).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;

  header.append(title, subtitle, expiry);
  root.append(header);

  const list = document.createElement("div");
  list.style.cssText = "display:flex;flex-direction:column;gap:0.625rem;";

  for (const item of items) {
    const row = document.createElement("article");
    row.className = "adjust-item fade-up d2";

    const top = document.createElement("div");
    top.className = "adjust-item-top";

    const matchClass = item.matchType === "exact"
      ? "adjust-match-badge--exact"
      : item.matchType === "fuzzy"
        ? "adjust-match-badge--fuzzy"
        : "adjust-match-badge--unknown";

    const typeLabel = document.createElement("span");
    typeLabel.className = `adjust-match-badge ${matchClass}`;
    typeLabel.textContent = item.matchType === "exact" ? "точно" : item.matchType === "fuzzy" ? "приблизительно" : "неизвестно";

    const conf = document.createElement("span");
    conf.className = "adjust-confidence";
    conf.textContent = confidenceLabelRu(item.confidence);
    top.append(typeLabel, conf);

    const nameInput = document.createElement("input");
    nameInput.className = "input-organic";
    nameInput.style.marginTop = "0.75rem";
    nameInput.value = item.editedName || item.name;
    nameInput.addEventListener("input", () => {
      item.editedName = nameInput.value;
    });

    const weightLabel = document.createElement("p");
    weightLabel.className = "adjust-weight-label";
    weightLabel.textContent = `Вес: ${item.weightG || 150} г`;

    const slider = document.createElement("input");
    slider.type = "range";
    slider.min = "1";
    slider.max = "1500";
    slider.step = "1";
    slider.value = String(item.weightG || 150);
    slider.style.cssText = "width:100%;margin-top:0.375rem;accent-color:var(--sage);";

    slider.addEventListener("input", () => {
      item.weightG = Number(slider.value);
      weightLabel.textContent = `Вес: ${item.weightG} г`;
    });

    row.append(top, nameInput, weightLabel, slider);

    if (Number(item.confidence || 0) < 0.5) {
      const low = document.createElement("p");
      low.className = "adjust-low-conf-hint";
      low.textContent = "Низкая уверенность — уточните название и вес.";
      row.append(low);
    }

    list.append(row);
  }

  root.append(list);
  root.append(
    createPrimaryButton("Подтвердить и рассчитать", submitAnalysisStep2, {
      disabled: state.busy || !items.length,
      loading: state.busy,
    }),
  );
  return root;
}

function renderAnalysisSummaryScreen() {
  const root = createRoot();
  const payload = state.analysisStep2Result;
  if (!payload?.meal?.result) {
    const empty = document.createElement("p");
    empty.className = "result-empty";
    empty.textContent = "Нет данных анализа";
    root.append(empty);
    return root;
  }

  root.append(createResultCard(payload.meal.result, {
    mealTime: payload.meal.mealTime,
    isPremium: state.subscription?.status === "active",
    hideWarningChip: false,
  }));

  const feedback = document.createElement("section");
  feedback.className = "feedback-card fade-up d3";
  const q = document.createElement("p");
  q.className = "feedback-card-q";
  q.textContent = "Распознавание было верным?";
  const actions = document.createElement("div");
  actions.className = "feedback-card-actions";
  actions.append(
    createSecondaryButton("Да", () => {
      state.analysisFeedback = "yes";
      showToast("Спасибо за обратную связь", "info");
    }),
    createSecondaryButton("Нет", () => {
      state.analysisFeedback = "no";
      showToast("Поняли, улучшим распознавание", "info");
    }),
  );
  feedback.append(q, actions);
  root.append(feedback);
  return root;
}

function createAnalyzeSkeletonScreen() {
  const section = document.createElement("section");
  section.style.cssText = "display:flex;flex-direction:column;gap:1rem;";

  const hero = document.createElement("section");
  hero.className = "glass analytics-card";

  const heroTitle = document.createElement("div");
  heroTitle.className = "skeleton-shimmer skeleton-line";
  heroTitle.style.cssText = "height:0.75rem;width:6rem;border-radius:6px;";

  const heroValue = document.createElement("div");
  heroValue.className = "skeleton-shimmer skeleton-line";
  heroValue.style.cssText = "height:2rem;width:8rem;border-radius:8px;margin-top:0.75rem;";

  const heroCaption = document.createElement("div");
  heroCaption.className = "skeleton-shimmer skeleton-line";
  heroCaption.style.cssText = "height:0.75rem;width:10rem;border-radius:6px;margin-top:0.625rem;";

  const macros = document.createElement("div");
  macros.style.cssText = "display:grid;grid-template-columns:repeat(3,1fr);gap:0.5rem;margin-top:1rem;";
  for (let i = 0; i < 3; i += 1) {
    const macro = document.createElement("div");
    macro.className = "stat-block";

    const macroLabel = document.createElement("div");
    macroLabel.className = "skeleton-shimmer skeleton-line";
    macroLabel.style.cssText = "height:0.6875rem;width:3rem;border-radius:4px;margin:0 auto;";

    const macroValue = document.createElement("div");
    macroValue.className = "skeleton-shimmer skeleton-line";
    macroValue.style.cssText = "height:1rem;width:4rem;border-radius:6px;margin:0.375rem auto 0;";

    macro.append(macroLabel, macroValue);
    macros.append(macro);
  }

  hero.append(heroTitle, heroValue, heroCaption, macros);

  const list = document.createElement("div");
  list.style.cssText = "display:flex;flex-direction:column;gap:0.625rem;";
  for (let i = 0; i < 2; i += 1) {
    const card = document.createElement("article");
    card.className = "glass";
    card.style.cssText = "padding:1rem;";

    const lineTop = document.createElement("div");
    lineTop.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:0.5rem;";

    const left = document.createElement("div");
    left.className = "skeleton-shimmer skeleton-line";
    left.style.cssText = "height:1rem;width:7rem;border-radius:6px;";

    const right = document.createElement("div");
    right.className = "skeleton-shimmer skeleton-line";
    right.style.cssText = "height:1.5rem;width:4rem;border-radius:var(--r-pill);";

    lineTop.append(left, right);

    const lineBottom = document.createElement("div");
    lineBottom.className = "skeleton-shimmer skeleton-line";
    lineBottom.style.cssText = "height:0.6875rem;width:11rem;border-radius:4px;margin-top:0.75rem;";

    card.append(lineTop, lineBottom);
    list.append(card);
  }

  const loadingText = document.createElement("p");
  loadingText.className = "loading-text";
  loadingText.style.textAlign = "center";
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
  loadingCard.className = "glass loading-card";

  const icon = document.createElement("div");
  icon.className = "loading-icon";
  icon.textContent = "🌿";

  const loading = document.createElement("p");
  loading.className = "loading-text";
  loading.textContent = state.loadingText || "Подключаемся к серверу";

  const spinner = document.createElement("div");
  spinner.className = "loading-spinner-organic";

  loadingCard.append(icon, loading, spinner);
  root.append(loadingCard);
  return root;
}

function renderAuthScreen() {
  const root = createRoot();
  const panel = document.createElement("section");
  panel.className = "glass auth-card fade-up d1";

  const icon = document.createElement("div");
  icon.style.cssText = "font-size:3rem;text-align:center;margin-bottom:1rem;animation:float-anim 4s ease-in-out infinite;";
  icon.textContent = "🌿";

  const title = document.createElement("h2");
  title.className = "title-serif";
  title.style.cssText = "font-size:1.5rem;text-align:center;margin-bottom:0.5rem;";
  title.textContent = "FitAI";

  const text = document.createElement("p");
  text.className = state.authErrorMessage ? "auth-error-text" : "auth-hint-text";
  text.textContent = state.authErrorMessage || "Откройте приложение через Telegram";

  const retryBtn = createPrimaryButton("Повторить", async () => {
    if (state.busy) {
      return;
    }
    await bootstrapAuth();
  }, {
    disabled: state.busy,
    loading: state.busy,
  });
  retryBtn.style.marginTop = "1rem";

  panel.append(icon, title, text, retryBtn);
  root.append(panel);
  return root;
}

function renderOnboardingScreen() {
  const root = createRoot();

  const header = document.createElement("div");
  header.className = "onboarding-header fade-up d1";

  const icon = document.createElement("div");
  icon.style.cssText = "font-size:2.5rem;text-align:center;margin-bottom:0.75rem;";
  icon.textContent = "🌱";

  const title = document.createElement("h2");
  title.className = "title-serif";
  title.style.cssText = "font-size:1.5rem;text-align:center;margin-bottom:0.25rem;";
  title.textContent = "Расскажите о себе";

  const subtitle = document.createElement("p");
  subtitle.style.cssText = "font-size:0.8125rem;text-align:center;color:var(--bark);opacity:0.5;";
  subtitle.textContent = "Нужно заполнить один раз";

  header.append(icon, title, subtitle);
  root.append(header);

  const form = document.createElement("form");
  form.className = "glass onboarding-form fade-up d2";

  const gender = document.createElement("select");
  gender.className = "input-organic";
  [["male", "Мужской"], ["female", "Женский"]].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    gender.append(option);
  });

  const age = document.createElement("input");
  age.className = "input-organic";
  age.type = "number";
  age.min = "10";
  age.max = "120";
  age.value = "24";
  age.placeholder = "Возраст";

  const heightCm = document.createElement("input");
  heightCm.className = "input-organic";
  heightCm.type = "number";
  heightCm.min = "80";
  heightCm.max = "250";
  heightCm.value = "170";
  heightCm.placeholder = "Рост, см";

  const weightKg = document.createElement("input");
  weightKg.className = "input-organic";
  weightKg.type = "number";
  weightKg.min = "20";
  weightKg.max = "400";
  weightKg.step = "0.1";
  weightKg.value = "70";
  weightKg.placeholder = "Вес, кг";

  const goal = document.createElement("select");
  goal.className = "input-organic";
  [["lose_weight", "Снижение веса"], ["maintain", "Поддержание"], ["gain_weight", "Набор массы"]].forEach(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    goal.append(option);
  });

  form.append(
    createFormField({ label: "Пол", input: gender }),
    createFormField({ label: "Возраст", input: age }),
    createFormField({ label: "Рост (см)", input: heightCm }),
    createFormField({ label: "Вес (кг)", input: weightKg }),
    createFormField({ label: "Цель", input: goal }),
  );

  const submit = createPrimaryButton("Начать →", () => {
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

  // Upload zone
  const uploader = document.createElement("section");
  uploader.className = "fade-up d2";

  const remaining = state.usage?.remaining ?? state.subscription?.remainingToday ?? 0;
  const isLimitExhausted = remaining <= 0;

  const fileInput = document.createElement("input");
  fileInput.style.display = "none";
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.capture = "environment";
  fileInput.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    handleAnalyze(file);
    event.target.value = "";
  });

  if (isLimitExhausted) {
    // Show paywall banner instead of upload zone
    const banner = document.createElement("div");
    banner.className = "premium-gate-card";
    banner.innerHTML = `
      <div class="premium-gate-badge">✦ PREMIUM</div>
      <p class="premium-gate-title">Лимит исчерпан</p>
      <p style="font-size:0.8125rem;color:rgba(255,255,255,0.55);margin-top:0.375rem;position:relative;">
        Оформите Premium, чтобы продолжить — до 20 фото в день
      </p>
      <div class="premium-gate-price" style="margin-top:0.875rem;">
        <span class="premium-gate-price-old">1499 ₽</span>
        <span class="premium-gate-price-current">499 ₽</span>
        <span class="premium-gate-price-period">/ 30 дней</span>
      </div>`;
    const bannerBtn = document.createElement("button");
    bannerBtn.type = "button";
    bannerBtn.style.cssText = "margin-top:1rem;width:100%;padding:0.75rem;background:rgba(255,255,255,0.18);border:1px solid rgba(255,255,255,0.3);border-radius:var(--r-md);font-family:'DM Sans',sans-serif;font-size:0.875rem;font-weight:600;color:white;cursor:pointer;position:relative;";
    bannerBtn.textContent = "Получить Premium";
    bannerBtn.addEventListener("click", () => {
      state.screen = "paywall";
      render();
    });
    banner.append(bannerBtn);
    uploader.append(banner);
  } else {
    // Upload zone
    const zone = document.createElement("div");
    zone.className = "upload-zone";
    zone.setAttribute("role", "button");
    zone.tabIndex = 0;

    const iconBox = document.createElement("div");
    iconBox.className = "upload-icon-box";
    iconBox.textContent = "📷";

    const uploadTitle = document.createElement("p");
    uploadTitle.className = "upload-title";
    uploadTitle.textContent = "Сфотографируйте блюдо";

    const uploadSub = document.createElement("p");
    uploadSub.className = "upload-sub";
    uploadSub.textContent = "Оценка калорий и БЖУ за пару секунд";

    zone.append(iconBox, uploadTitle, uploadSub);
    zone.addEventListener("click", () => {
      fileInput.click();
    });
    zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") zone.click(); });

    uploader.append(zone);
  }

  uploader.append(fileInput);

  // Main CTA button (only if not limit exhausted)
  if (!isLimitExhausted) {
    const uploadBtn = createPrimaryButton(
      state.analyzing ? "Анализируем..." : "Загрузить фото",
      () => {
        fileInput.click();
      },
      {
        disabled: state.busy || state.analyzing,
        loading: state.analyzing,
        icon: "camera",
      },
    );
    uploader.append(uploadBtn);
  }

  root.append(uploader);

  if (state.usage) {
    root.append(createQuotaLabel(state.usage));
  }

  // ===== Charts Section =====
  if (state.weeklyStats && state.weeklyStats.days && state.weeklyStats.days.length > 0) {
    const calCard = document.createElement("section");
    calCard.className = "glass fade-up d3";
    calCard.style.padding = "1.25rem";
    calCard.style.marginTop = "1rem";
    
    const calHeader = document.createElement("div");
    calHeader.style.display = "flex";
    calHeader.style.justifyContent = "space-between";
    calHeader.style.alignItems = "baseline";
    calHeader.style.marginBottom = "1rem";
    
    const calTitle = document.createElement("p");
    calTitle.className = "analytics-label";
    calTitle.textContent = "Калории за неделю";
    
    const calSub = document.createElement("p");
    calSub.style.fontSize = "0.75rem";
    calSub.style.color = "var(--bark)";
    calSub.style.opacity = "0.6";
    calSub.textContent = "ккал/день";
    
    calHeader.append(calTitle, calSub);
    calCard.append(calHeader);
    
    const chartWrap = document.createElement("div");
    chartWrap.style.display = "flex";
    chartWrap.style.alignItems = "flex-end";
    chartWrap.style.justifyContent = "space-between";
    chartWrap.style.height = "100px";
    chartWrap.style.gap = "4px";
    
    const days = state.weeklyStats.days;
    const maxCal = Math.max(1, ...days.map(d => d.calories_kcal || 0));
    const goalCal = getDailyTarget(state.user?.profile) || 2000;
    const chartMax = Math.max(maxCal, goalCal * 1.2); // Give some headroom above goal
    
    days.forEach(day => {
      const col = document.createElement("div");
      col.style.display = "flex";
      col.style.flexDirection = "column";
      col.style.alignItems = "center";
      col.style.flex = "1";
      col.style.gap = "4px";
      
      const barWrapBox = document.createElement("div");
      barWrapBox.style.position = "relative";
      barWrapBox.style.width = "100%";
      barWrapBox.style.height = "100%";
      barWrapBox.style.display = "flex";
      barWrapBox.style.alignItems = "flex-end";
      barWrapBox.style.justifyContent = "center";
      barWrapBox.style.borderRadius = "4px";
      barWrapBox.style.backgroundColor = "rgba(0,0,0,0.03)";
      
      const val = day.calories_kcal || 0;
      const pct = (val / chartMax) * 100;
      const normalizedPct = val > 0 ? Math.max(8, pct) : 4;
      
      const bar = document.createElement("div");
      bar.style.width = "100%";
      bar.style.maxWidth = "24px";
      bar.style.height = `${Math.min(100, normalizedPct)}%`;
      bar.style.backgroundColor = val > goalCal ? "var(--clay)" : "var(--sage)";
      bar.style.borderRadius = "4px";
      bar.style.transition = "height 0.3s ease";
      
      barWrapBox.append(bar);
      
      const lbl = document.createElement("span");
      lbl.style.fontSize = "0.625rem";
      lbl.style.color = "var(--bark)";
      lbl.style.opacity = "0.6";
      const dateObj = new Date(`${day.date}T00:00:00Z`);
      const dayNames = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
      const dayIndex = Number.isNaN(dateObj.getTime()) ? 0 : dateObj.getUTCDay();
      lbl.textContent = dayNames[dayIndex] || "-";
      
      col.append(barWrapBox, lbl);
      chartWrap.append(col);
    });
    
    calCard.append(chartWrap);
    root.append(calCard);
  } else {
    const calCard = document.createElement("section");
    calCard.className = "glass fade-up d3";
    calCard.style.padding = "1.25rem";
    calCard.style.marginTop = "1rem";
    const title = document.createElement("p");
    title.className = "analytics-label";
    title.textContent = "Калории за неделю";
    const hint = document.createElement("p");
    hint.className = "analytics-hint";
    hint.style.marginTop = "0.5rem";
    hint.textContent = "Пока нет данных для графика.";
    calCard.append(title, hint);
    root.append(calCard);
  }

  // Weight Chart
  const weightItems = Array.isArray(state.weightChart?.items) ? [...state.weightChart.items] : [];
  const onboardingWeight = getProfileWeightKg();
  if (!weightItems.length && Number.isFinite(onboardingWeight)) {
    weightItems.push({
      date: getTodayUtcDate(),
      weight: onboardingWeight,
    });
  }

  if (weightItems.length > 0) {
    const weightCard = document.createElement("section");
    weightCard.className = "glass fade-up d3";
    weightCard.style.padding = "1.25rem";
    weightCard.style.marginTop = "1rem";
    
    const weightHeader = document.createElement("div");
    weightHeader.style.display = "flex";
    weightHeader.style.justifyContent = "space-between";
    weightHeader.style.alignItems = "baseline";
    weightHeader.style.marginBottom = "1rem";
    
    const weightTitle = document.createElement("p");
    weightTitle.className = "analytics-label";
    weightTitle.textContent = "Динамика веса";
    
    const weightAddBtn = document.createElement("button");
    weightAddBtn.type = "button";
    weightAddBtn.className = "inline-weight-btn";
    weightAddBtn.textContent = "+ Вес";
    weightAddBtn.addEventListener("click", handleAddWeight);
    
    weightHeader.append(weightTitle, weightAddBtn);
    weightCard.append(weightHeader);
    
    const items = weightItems;
    
    const width = 320;
    const height = 100;
    const padding = 10;
    const path = buildChartPath(items, width, height, padding);
    
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.style.width = "100%";
    svg.style.height = "auto";
    svg.style.overflow = "visible";
    
    const poly = document.createElementNS("http://www.w3.org/2000/svg", "path");
    poly.setAttribute("d", path);
    poly.setAttribute("fill", "none");
    poly.setAttribute("stroke", "var(--sage)");
    poly.setAttribute("stroke-width", "3");
    poly.setAttribute("stroke-linecap", "round");
    poly.setAttribute("stroke-linejoin", "round");
    
    svg.append(poly);
    weightCard.append(svg);
    
    const foot = document.createElement("p");
    foot.className = "analytics-hint";
    foot.style.marginTop = "0.75rem";
    const first = items[0];
    const last = items[items.length - 1];
    foot.textContent = `${formatDayDate(first?.date)}: ${formatMetric(first?.weight, 1)} кг → ${formatDayDate(last?.date)}: ${formatMetric(last?.weight, 1)} кг`;
    weightCard.append(foot);
    
    root.append(weightCard);
  } else {
    // Empty state for weight
    const weightCard = document.createElement("section");
    weightCard.className = "glass fade-up d3";
    weightCard.style.padding = "1.25rem";
    weightCard.style.marginTop = "1rem";
    weightCard.style.display = "flex";
    weightCard.style.justifyContent = "space-between";
    weightCard.style.alignItems = "center";
    
    const weightTitle = document.createElement("p");
    weightTitle.className = "analytics-label";
    weightTitle.textContent = "Динамика веса";
    
    const weightAddBtn = document.createElement("button");
    weightAddBtn.type = "button";
    weightAddBtn.className = "inline-weight-btn";
    weightAddBtn.textContent = "+ Внести первый вес";
    weightAddBtn.addEventListener("click", handleAddWeight);
    
    weightCard.append(weightTitle, weightAddBtn);
    root.append(weightCard);
  }

  // Actions grid
  const actions = document.createElement("div");
  actions.className = "action-grid fade-up d3";
  actions.append(
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
    createSecondaryButton("Поделиться", openShareScreen, { disabled: state.busy, icon: "share" }),
  );

  const historyWrap = document.createElement("div");
  historyWrap.className = "fade-up d3";
  historyWrap.style.marginTop = "0.75rem";
  historyWrap.append(createSecondaryButton("История", openHistory, { disabled: state.busy, icon: "history" }));

  root.append(actions);
  root.append(historyWrap);
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
    empty.className = "result-empty";
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
  card.className = "glass analytics-card";
  card.innerHTML = `<p class="analytics-loading">Загружаем данные...</p>`;
  return card;
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

function renderPaywallScreen() {
  if (state.subscription?.status === "active") {
    state.screen = "main";
    return renderMainScreen();
  }

  const root = createRoot();
  root.append(createPremiumGateCard({
    onCta: handleUpgrade,
    note: "Лимит Premium: 20 фото в день.",
  }));

  root.append(
    createPrimaryButton("Получить Premium за 499 ₽", handleUpgrade, {
      disabled: state.busy,
      loading: state.busy,
      icon: "crown",
    }),
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
  const isActive = sub.status === "active";

  const card = document.createElement("section");
  card.className = "glass analytics-card fade-up d1";

  const badge = document.createElement("div");
  badge.className = isActive ? "sub-status-badge sub-status-badge--active" : "sub-status-badge sub-status-badge--free";
  badge.textContent = isActive ? "✦ Premium активен" : `Статус: ${sub.status || "free"}`;

  const grid = document.createElement("div");
  grid.className = "analytics-grid";
  grid.append(
    createSecondaryInfo("Активна до", sub.activeUntil ? new Date(sub.activeUntil).toLocaleDateString("ru-RU") : "-"),
    createSecondaryInfo("Стоимость", "499 ₽ / 30 дней"),
    createSecondaryInfo("Лимит", `${sub.dailyLimit ?? 0} фото/день`),
    createSecondaryInfo("Использовано", `${sub.usedToday ?? 0}`),
    createSecondaryInfo("Осталось", `${sub.remainingToday ?? 0}`),
  );

  card.append(badge, grid);
  root.append(card);

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

function ensureToastMount() {
  if (document.getElementById("toast")) {
    return;
  }
  const toast = document.createElement("div");
  toast.id = "toast";
  toast.className = "toast-base toast-hidden alert-error";
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
  loader.className = "screen-loader";
  const spinner = document.createElement("div");
  spinner.className = "screen-loader-spinner";
  loader.append(spinner);
  return loader;
}

function renderWeightEntryModal() {
  const overlay = document.createElement("div");
  overlay.className = "weight-entry-overlay";
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      closeWeightEntry();
    }
  });

  const modal = document.createElement("section");
  modal.className = "weight-entry-card";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  const badge = document.createElement("p");
  badge.className = "weight-entry-badge";
  badge.textContent = "вес";

  const title = document.createElement("h3");
  title.className = "weight-entry-title";
  title.textContent = "Добавьте текущий вес";

  const hint = document.createElement("p");
  hint.className = "weight-entry-hint";
  hint.textContent = "Нужно для красивого графика и точных рекомендаций";

  const inputWrap = document.createElement("label");
  inputWrap.className = "weight-entry-input-wrap";

  const input = document.createElement("input");
  input.className = "weight-entry-input";
  input.type = "number";
  input.min = "20";
  input.max = "400";
  input.step = "0.1";
  input.placeholder = "70.0";
  input.value = state.weightEntryValue;
  input.addEventListener("input", () => {
    state.weightEntryValue = input.value;
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitWeightEntry();
    }
  });

  const unit = document.createElement("span");
  unit.className = "weight-entry-unit";
  unit.textContent = "кг";
  inputWrap.append(input, unit);

  const actions = document.createElement("div");
  actions.className = "weight-entry-actions";
  const save = createPrimaryButton("Сохранить", submitWeightEntry, {
    disabled: state.weightEntrySaving,
    loading: state.weightEntrySaving,
  });
  save.classList.add("weight-entry-save");
  const cancel = createSecondaryButton("Отмена", closeWeightEntry, {
    disabled: state.weightEntrySaving,
  });
  cancel.classList.add("weight-entry-cancel");
  actions.append(save, cancel);

  modal.append(badge, title, hint, inputWrap, actions);
  overlay.append(modal);
  return overlay;
}

function closeGoalEntry() {
  if (state.goalEntrySaving) {
    return;
  }
  state.goalEntryOpen = false;
  render();
}

async function submitGoalEntry() {
  if (state.goalEntrySaving) {
    return;
  }
  const parsed = parseInt(String(state.goalEntryValue || ""), 10);
  if (!Number.isFinite(parsed) || parsed < 800 || parsed > 6000) {
    showToast("Пожалуйста, введите число от 800 до 6000");
    return;
  }

  state.goalEntrySaving = true;

  const withTimeout = (promise, timeoutMs = 15000) => Promise.race([
    promise,
    new Promise((_, reject) => {
      window.setTimeout(() => reject(new Error("timeout")), timeoutMs);
    }),
  ]);

  state.goalEntryOpen = false;
  render();

  try {
    const res = await withTimeout(updateProfileGoal(parsed));
    if (!state.user.profile) {
      state.user.profile = {};
    }
    state.user.profile.dailyGoal = Number(res?.dailyGoal || parsed);
    showToast("Цель обновлена", "info");
    refreshUsageAndSubscription().then(render).catch(() => {});
  } catch (err) {
    if (err instanceof Error && err.message === "timeout") {
      showToast("Сохранение цели заняло слишком много времени");
    } else {
      showToast(mapFriendlyError(err));
    }
  } finally {
    state.goalEntrySaving = false;
    render();
  }
}

function renderGoalEntryModal() {
  const overlay = document.createElement("div");
  overlay.className = "weight-entry-overlay";
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      closeGoalEntry();
    }
  });

  const modal = document.createElement("section");
  modal.className = "weight-entry-card";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.addEventListener("click", (event) => {
    event.stopPropagation();
  });

  const badge = document.createElement("p");
  badge.className = "weight-entry-badge";
  badge.textContent = "цель";

  const title = document.createElement("h3");
  title.className = "weight-entry-title";
  title.textContent = "Измените дневную норму";

  const hint = document.createElement("p");
  hint.className = "weight-entry-hint";
  hint.textContent = "Допустимый диапазон: 800–6000 ккал";

  const inputWrap = document.createElement("label");
  inputWrap.className = "weight-entry-input-wrap";

  const input = document.createElement("input");
  input.className = "weight-entry-input";
  input.type = "number";
  input.min = "800";
  input.max = "6000";
  input.step = "1";
  input.placeholder = "2000";
  input.value = state.goalEntryValue;
  input.addEventListener("input", () => {
    state.goalEntryValue = input.value;
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitGoalEntry();
    }
  });

  const unit = document.createElement("span");
  unit.className = "weight-entry-unit";
  unit.textContent = "ккал";
  inputWrap.append(input, unit);

  const actions = document.createElement("div");
  actions.className = "weight-entry-actions";
  const save = createPrimaryButton("Сохранить", submitGoalEntry, {
    disabled: state.goalEntrySaving,
    loading: state.goalEntrySaving,
  });
  save.classList.add("weight-entry-save");
  const cancel = createSecondaryButton("Отмена", closeGoalEntry, {
    disabled: state.goalEntrySaving,
  });
  cancel.classList.add("weight-entry-cancel");
  actions.append(save, cancel);

  modal.append(badge, title, hint, inputWrap, actions);
  overlay.append(modal);
  return overlay;
}

function createSecondaryInfo(label, value) {
  const box = document.createElement("div");
  box.className = "info-box";

  const l = document.createElement("div");
  l.className = "info-box-label";
  l.textContent = label;

  const v = document.createElement("div");
  v.className = "info-box-value";
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
  actions.style.cssText = "display:flex;align-items:center;gap:0.5rem;";

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
    case "analysisAdjust":
      return { title: "Уточните вес", subtitle: "Шаг 2 из 2" };
    case "analysisSummary":
      return { title: "Итог анализа", subtitle: "Расчёт по вашим данным" };
    case "history":
      return { title: "История", subtitle: "Последние приемы пищи" };
    case "historyDetail":
      return { title: "Прием пищи", subtitle: "Детали анализа" };
    case "paywall":
      return { title: "Открыть Premium", subtitle: "Больше фото, меньше ограничений" };
    case "subscription":
      return { title: "Подписка", subtitle: "Текущий статус" };
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
  try {
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
      case "analysisAdjust":
        screenNode = renderAnalysisAdjustScreen();
        break;
      case "analysisSummary":
        screenNode = renderAnalysisSummaryScreen();
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
      case "share":
        screenNode = renderShareScreen();
        break;
      case "loading":
      default:
        screenNode = renderLoadingScreen();
        break;
    }
  } catch (error) {
    console.error("RENDER_FATAL", { screen: state.screen, error });
    const fallback = document.createElement("section");
    fallback.className = "glass auth-card";
    const title = document.createElement("p");
    title.className = "auth-error-text";
    title.textContent = "Ошибка интерфейса";
    const details = document.createElement("p");
    details.className = "auth-hint-text";
    const message = error instanceof Error ? error.message : "unknown";
    details.textContent = `screen=${state.screen}; ${message}`;
    fallback.append(title, details);
    screenNode = fallback;
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

  if (state.weightEntryOpen) {
    app.append(renderWeightEntryModal());
  }

  if (state.goalEntryOpen) {
    app.append(renderGoalEntryModal());
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

async function bootstrapApp() {
  if (state.token) {
    const loaded = await bootstrapUser();
    if (loaded) {
      return;
    }
  }
  await bootstrapAuth();
}

bootstrapApp();
