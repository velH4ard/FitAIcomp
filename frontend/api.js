const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const APP_ENV = import.meta.env.VITE_APP_ENV ?? import.meta.env.MODE;
const IS_DEV = APP_ENV !== "production";

let tokenGetter = () => localStorage.getItem("fitai.accessToken");
let unauthorizedHandler = () => {};
let tokenInvalidator = () => {};
let silentReauthHandler = async () => false;

export class ApiError extends Error {
  constructor({ code = "INTERNAL_ERROR", message = "Ошибка", details = {}, status = 500 }) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.details = details;
    this.status = status;
  }
}

export function setTokenGetter(getter) {
  tokenGetter = getter;
}

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

export function setTokenInvalidator(handler) {
  tokenInvalidator = handler;
}

export function setSilentReauthHandler(handler) {
  silentReauthHandler = handler;
}

function createRequestId() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createUuid() {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeReminderTone(value) {
  const tone = String(value || "").trim().toLowerCase();
  if (tone === "soft" || tone === "balanced" || tone === "hard") {
    return tone;
  }
  return null;
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

async function request(path, options = {}, retryCount = 0) {
  const {
    method = "GET",
    body,
    auth = true,
    isMultipart = false,
    headers = {},
  } = options;

  const requestHeaders = {
    "X-Request-Id": createRequestId(),
    ...headers,
  };

  const token = tokenGetter();
  if (auth && token) {
    requestHeaders.Authorization = `Bearer ${token}`;
  }

  let payload = body;
  if (body && !isMultipart && !(body instanceof FormData)) {
    requestHeaders["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: requestHeaders,
      body: payload,
    });
  } catch (_error) {
    throw new ApiError({
      code: "NETWORK_ERROR",
      message: "Проблема соединения",
      details: {},
      status: 0,
    });
  }

  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await response.json() : null;

  if (response.ok) {
    return data;
  }

  const errorBody = data?.error || {};
  const apiError = new ApiError({
    code: errorBody.code || "INTERNAL_ERROR",
    message: errorBody.message || "Попробуйте позже",
    details: errorBody.details || {},
    status: response.status,
  });

  const isUnauthorized = response.status === 401 || apiError.code === "UNAUTHORIZED";

  if (isUnauthorized && auth && retryCount === 0) {
    tokenInvalidator();
    const reauthOk = await silentReauthHandler();
    if (reauthOk) {
      if (IS_DEV) {
        console.info("silent reauth success");
      }
      return request(path, options, 1);
    }
    unauthorizedHandler(apiError);
  } else if (auth && (isUnauthorized || apiError.code === "UNAUTHORIZED")) {
    unauthorizedHandler(apiError);
  }

  throw apiError;
}

export function authTelegram(initData) {
  return request("/v1/auth/telegram", {
    method: "POST",
    body: { initData },
    auth: false,
  });
}

export function getMe() {
  return request("/v1/me");
}

export function updateProfile(profile) {
  return request("/v1/me/profile", {
    method: "PUT",
    body: profile,
  });
}

export function getUsageToday() {
  return request("/v1/usage/today");
}

export function analyzeMeal(imageFile, options = {}) {
  const description = typeof options.description === "string" ? options.description.trim() : "";
  const formData = new FormData();
  formData.append("image", imageFile);
  if (description) {
    formData.append("description", description);
  }

  return request("/v1/meals/analyze", {
    method: "POST",
    body: formData,
    isMultipart: true,
    headers: {
      "Idempotency-Key": createUuid(),
    },
  });
}

export function getStatsDaily(date) {
  return request(`/v1/stats/daily?date=${encodeURIComponent(date)}`);
}

export function getMeals(limit = 20) {
  return request(`/v1/meals?limit=${encodeURIComponent(limit)}`);
}

export function getMealById(mealId) {
  return request(`/v1/meals/${encodeURIComponent(mealId)}`);
}

export function getSubscription() {
  return request("/v1/subscription");
}

export function getWeeklyReport() {
  return request("/v1/reports/weekly");
}

export function getMonthlyReport() {
  return request("/v1/reports/monthly");
}

export function getWhyNotLosing() {
  return request("/v1/analysis/why-not-losing");
}

export function getWeightChart() {
  return request("/v1/charts/weight");
}

export function updateProfileGoal(dailyGoal) {
  return request("/v1/profile/goal", {
    method: "PATCH",
    body: {
      dailyGoal,
    },
  });
}

export async function patchNotificationSettings(payloadOrEnabled, toneOverride) {
  const payload = {
    enabled: true,
  };

  if (typeof payloadOrEnabled === "boolean") {
    payload.enabled = payloadOrEnabled;
    const normalizedTone = normalizeReminderTone(toneOverride);
    if (normalizedTone) {
      payload.tone = normalizedTone;
    }
  } else {
    const enabled = readReminderEnabled(payloadOrEnabled);
    if (typeof enabled === "boolean") {
      payload.enabled = enabled;
    }

    const normalizedTone = normalizeReminderTone(payloadOrEnabled?.tone ?? payloadOrEnabled?.reminderTone);
    if (normalizedTone) {
      payload.tone = normalizedTone;
    }
  }

  const response = await request("/v1/notifications/settings", {
    method: "PATCH",
    body: payload,
  });

  const normalizedResponse = {
    ...(response && typeof response === "object" ? response : {}),
  };
  const responseEnabled = readReminderEnabled(response);
  if (typeof responseEnabled === "boolean") {
    normalizedResponse.enabled = responseEnabled;
  }

  const responseTone = readReminderTone(response);
  if (responseTone) {
    normalizedResponse.tone = responseTone;
  }

  return normalizedResponse;
}

export function createYookassaPayment(returnUrl) {
  return request("/v1/subscription/yookassa/create", {
    method: "POST",
    body: {
      returnUrl,
      idempotencyKey: createUuid(),
    },
  });
}

export function refreshYookassaPayment(paymentId) {
  return request("/v1/subscription/yookassa/refresh", {
    method: "POST",
    body: {
      paymentId,
    },
  });
}

export function getStreak() {
  return request("/v1/streak");
}
