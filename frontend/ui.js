function formatDateTime(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
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

export const QUOTES_ALL = [
  "Маленькие шаги каждый день дают большой результат.",
  "Ты ближе к цели, чем думаешь.",
  "Сегодня — ещё один день стать лучше.",
  "Дисциплина > мотивация.",
  "Результат строится из привычек.",
  "Главное — не идеальность, а регулярность.",
  "Ты управляешь своим прогрессом.",
  "Один приём пищи может изменить день.",
  "Тело благодарит за внимание.",
  "Стабильность побеждает всплески.",
  "Не сдавайся, просто продолжай.",
  "Маленький контроль — большой эффект.",
  "Ты уже сделал больше, чем вчера.",
  "Твой ритм — твои правила.",
  "Осознанность — это сила.",
  "Прогресс — это процесс.",
  "Сегодняшний выбор формирует завтра.",
  "Делай лучше, чем вчера.",
  "Каждый день — шанс укрепить себя.",
  "Счётчик идёт. Продолжай.",
  "Ты строишь себя шаг за шагом.",
  "Не ищи идеальный момент — создавай его.",
  "Постоянство — твой суперпауэр.",
  "Контроль питания — контроль результата.",
  "Ты уже на пути. Не останавливайся.",
  "Хочешь результат — действуй.",
  "Слабость — это выбор.",
  "Делай, даже если не хочется.",
  "Никто не сделает это за тебя.",
  "Контроль начинается с дисциплины.",
  "Твой прогресс — твоя ответственность.",
  "Без усилий нет изменений.",
  "Привычки решают всё.",
  "Ты либо растёшь, либо стоишь.",
  "Сделай сегодня то, что другие откладывают.",
  "Ты заслуживаешь заботы о себе.",
  "Маленькие изменения — тоже прогресс.",
  "Будь терпелив к себе.",
  "Сегодня достаточно просто стараться.",
  "Слушай своё тело.",
  "Не сравнивай — двигайся в своём темпе.",
  "Забота о себе — это сила.",
  "Каждый шаг имеет значение.",
  "Ты уже делаешь больше, чем думаешь.",
  "Главное — продолжать мягко и уверенно.",
];

export const QUOTES_SHARE_SHORT = [
  "Дисциплина > мотивация.",
  "Прогресс — это процесс.",
  "Стабильность побеждает всплески.",
  "Привычки решают всё.",
  "Делай лучше, чем вчера.",
  "Ты ближе к цели.",
  "Продолжай. Шаг за шагом.",
  "Счётчик идёт. Продолжай.",
  "Осознанность — это сила.",
  "Каждый шаг важен.",
];

const QUOTE_LAST_SHOWN_KEY = "fitai_quote_last_shown_at";
const QUOTE_LAST_INDEX_KEY = "fitai_quote_last_index";
const QUOTE_COOLDOWN_MS = 5 * 60 * 60 * 1000;

function safeGetLocalStorageValue(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (_error) {
    return null;
  }
}

function safeSetLocalStorageValue(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (_error) {
    // ignore localStorage write errors
  }
}

export function getLastShownQuoteIndex() {
  const raw = safeGetLocalStorageValue(QUOTE_LAST_INDEX_KEY);
  const parsed = Number.parseInt(raw ?? "", 10);
  return Number.isInteger(parsed) ? parsed : -1;
}

export function shouldShowQuoteOverlay() {
  const raw = safeGetLocalStorageValue(QUOTE_LAST_SHOWN_KEY);
  const lastShownAt = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(lastShownAt) || lastShownAt <= 0) {
    return true;
  }
  return Date.now() - lastShownAt >= QUOTE_COOLDOWN_MS;
}

export function pickRandomQuote(list, avoidIndex = -1) {
  if (!Array.isArray(list) || list.length === 0) {
    return { text: "", index: -1 };
  }

  if (list.length === 1) {
    return { text: list[0], index: 0 };
  }

  let index = Math.floor(Math.random() * list.length);
  if (index === avoidIndex) {
    index = (index + 1 + Math.floor(Math.random() * (list.length - 1))) % list.length;
  }

  return {
    text: list[index],
    index,
  };
}

export function rememberQuoteOverlayShown(index) {
  safeSetLocalStorageValue(QUOTE_LAST_SHOWN_KEY, Date.now());
  if (Number.isInteger(index) && index >= 0) {
    safeSetLocalStorageValue(QUOTE_LAST_INDEX_KEY, index);
  }
}

export function renderQuoteOverlay(text, onDismiss) {
  const overlay = document.createElement("div");
  overlay.className = "quote-modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Мотивация");

  const card = document.createElement("div");
  card.className = "quote-modal-card";

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "quote-modal-close";
  closeButton.setAttribute("aria-label", "Закрыть");
  closeButton.textContent = "×";

  const label = document.createElement("p");
  label.className = "quote-modal-label";
  label.textContent = "Мотивация";

  const quote = document.createElement("p");
  quote.className = "quote-modal-text";
  quote.textContent = text;

  const subtext = document.createElement("p");
  subtext.className = "quote-modal-subtext";
  subtext.textContent = "Продолжай в своём ритме";

  const action = document.createElement("button");
  action.type = "button";
  action.className = "btn-primary-organic";
  action.textContent = "Понял(а)";

  const dismiss = () => {
    overlay.classList.remove("quote-modal-overlay--visible");
    card.classList.remove("quote-modal-card--visible");
    window.setTimeout(() => {
      overlay.remove();
      if (typeof onDismiss === "function") {
        onDismiss();
      }
    }, 180);
  };

  closeButton.addEventListener("click", dismiss);
  action.addEventListener("click", dismiss);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      dismiss();
    }
  });

  card.append(closeButton, label, quote, subtext, action);
  overlay.append(card);

  window.requestAnimationFrame(() => {
    overlay.classList.add("quote-modal-overlay--visible");
    card.classList.add("quote-modal-card--visible");
  });

  return overlay;
}

/** Compact stat block (value + label, for macro grids) */
function statBlock(title, value) {
  const card = document.createElement("div");
  card.className = "stat-block";

  const titleEl = document.createElement("div");
  titleEl.className = "stat-block-label";
  titleEl.textContent = title;

  const valueEl = document.createElement("div");
  valueEl.className = "stat-block-value";
  valueEl.textContent = value;

  card.append(valueEl, titleEl);
  return card;
}

const ITEM_NAME_TRANSLATIONS = {
  rice: "рис",
  chicken: "курица",
  beef: "говядина",
  pork: "свинина",
  fish: "рыба",
  salmon: "лосось",
  tuna: "тунец",
  egg: "яйцо",
  eggs: "яйца",
  bread: "хлеб",
  soup: "суп",
  salad: "салат",
  pasta: "паста",
  noodle: "лапша",
  noodles: "лапша",
  potato: "картофель",
  potatoes: "картофель",
  tomato: "помидор",
  cucumber: "огурец",
  cheese: "сыр",
  yogurt: "йогурт",
  porridge: "каша",
  buckwheat: "гречка",
  oatmeal: "овсянка",
  dumplings: "пельмени",
  pilaf: "плов",
  burger: "бургер",
  pizza: "пицца",
  fries: "картофель фри",
  cutlet: "котлета",
};

function toRussianItemName(name) {
  if (!name) {
    return "Блюдо";
  }

  const hasCyrillic = /[А-Яа-яЁё]/.test(name);
  if (hasCyrillic) {
    return name;
  }

  const normalized = String(name).trim().toLowerCase();
  if (!normalized) {
    return "Блюдо";
  }

  const direct = ITEM_NAME_TRANSLATIONS[normalized];
  if (direct) {
    return direct;
  }

  const translated = normalized
    .split(/\s+/)
    .map((word) => ITEM_NAME_TRANSLATIONS[word] || word)
    .join(" ");

  return translated;
}

function formatCalories(value) {
  const n = Number(value);
  const safe = Number.isFinite(n) ? n : 0;
  return Math.round(safe);
}

function formatMacro(value) {
  const n = Number(value);
  const safe = Number.isFinite(n) ? n : 0;
  return safe.toLocaleString("ru-RU", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  });
}

function mealTimeLabel(value) {
  const labels = {
    breakfast: "Завтрак",
    lunch: "Обед",
    dinner: "Ужин",
    snack: "Перекус",
    unknown: "—",
  };
  return labels[value] || "—";
}

export function createRoot() {
  const root = document.createElement("div");
  root.className = "page-root";
  return root;
}

export function createTitle(text, subtitle = "") {
  const wrap = document.createElement("div");

  const title = document.createElement("h1");
  title.className = "title-serif";
  title.style.fontSize = "1.5rem";
  title.textContent = text;

  wrap.append(title);

  if (subtitle) {
    const sub = document.createElement("p");
    sub.style.cssText = "margin-top:0.25rem;font-size:0.875rem;color:var(--bark);opacity:0.5;";
    sub.textContent = subtitle;
    wrap.append(sub);
  }

  return wrap;
}

function createLucideIcon(name, style = "width:1rem;height:1rem;") {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("fill", "none");
  icon.setAttribute("stroke", "currentColor");
  icon.setAttribute("stroke-width", "2");
  icon.setAttribute("stroke-linecap", "round");
  icon.setAttribute("stroke-linejoin", "round");
  icon.setAttribute("aria-hidden", "true");
  icon.style.cssText = style;

  const paths = {
    camera: [
      ["path", { d: "M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z" }],
      ["circle", { cx: "12", cy: "13", r: "3" }],
    ],
    history: [
      ["path", { d: "M3 3v5h5" }],
      ["path", { d: "M3.05 13A9 9 0 1 0 6 5.3L3 8" }],
      ["path", { d: "M12 7v5l4 2" }],
    ],
    crown: [
      ["path", { d: "m2 6 4.5 6L12 4l5.5 8L22 6l-2 14H4z" }],
    ],
    refresh: [
      ["path", { d: "M21 2v6h-6" }],
      ["path", { d: "M3 12a9 9 0 0 1 15-6l3 2" }],
      ["path", { d: "M3 22v-6h6" }],
      ["path", { d: "M21 12a9 9 0 0 1-15 6l-3-2" }],
    ],
    share: [
      ["path", { d: "M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8" }],
      ["polyline", { points: "16 6 12 2 8 6" }],
      ["line", { x1: "12", y1: "2", x2: "12", y2: "15" }],
    ],
    "arrow-left": [
      ["path", { d: "m12 19-7-7 7-7" }],
      ["path", { d: "M19 12H5" }],
    ],
  };

  for (const [tag, attrs] of paths[name] || []) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) {
      node.setAttribute(key, value);
    }
    icon.append(node);
  }

  return icon;
}

function createButton(text, onClick, className, options = {}) {
  const {
    disabled = false,
    loading = false,
    icon = "",
  } = options;
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.disabled = disabled || loading;

  if (icon) {
    button.append(createLucideIcon(icon, "width:1rem;height:1rem;"));
  }

  if (loading) {
    const spinner = document.createElement("span");
    spinner.className = "btn-spinner";
    spinner.setAttribute("aria-hidden", "true");
    button.append(spinner);
  }

  const label = document.createElement("span");
  label.textContent = text;
  button.append(label);

  button.addEventListener("click", onClick);
  return button;
}

export function createPrimaryButton(text, onClick, options = {}) {
  return createButton(text, onClick, "btn-primary-organic", options);
}

export function createSecondaryButton(text, onClick, options = {}) {
  return createButton(text, onClick, "btn-secondary-organic", options);
}

export function createIconButton({ icon, onClick, ariaLabel }) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn-back";
  button.setAttribute("aria-label", ariaLabel);
  button.append(createLucideIcon(icon, "width:1.25rem;height:1.25rem;"));
  button.addEventListener("click", onClick);
  return button;
}

export function createHeaderShell({ title = "", subtitle = "", left = null, right = null } = {}) {
  const header = document.createElement("div");
  header.className = "app-header-shell";

  const leftSlot = document.createElement("div");
  leftSlot.className = "app-header-slot";
  if (left) {
    leftSlot.append(left);
  }

  const center = document.createElement("div");
  center.style.cssText = "min-width: 0; text-align: center;";

  if (title) {
    const titleEl = document.createElement("h1");
    titleEl.className = "app-header-title";
    titleEl.textContent = title;
    center.append(titleEl);
  }

  if (subtitle) {
    const subtitleEl = document.createElement("p");
    subtitleEl.className = "app-header-subtitle";
    subtitleEl.textContent = subtitle;
    center.append(subtitleEl);
  }

  const rightSlot = document.createElement("div");
  rightSlot.className = "app-header-slot";
  rightSlot.style.justifyContent = "flex-end";
  if (right) {
    rightSlot.append(right);
  }

  header.append(leftSlot, center, rightSlot);
  return header;
}

export function createHeaderBackButton(onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn-back";
  button.setAttribute("aria-label", "Назад");
  button.append(createLucideIcon("arrow-left", "width:1.25rem;height:1.25rem;"));
  button.addEventListener("click", onClick);
  return button;
}

export function createProgressBar({ value = 0, max = 100, label = "Прогресс" } = {}) {
  const safeMax = Math.max(1, Number(max) || 1);
  const safeValue = Math.max(0, Math.min(safeMax, Number(value) || 0));
  const percent = (safeValue / safeMax) * 100;

  const track = document.createElement("div");
  track.className = "progress-track";
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", label);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(Math.round(safeMax)));
  track.setAttribute("aria-valuenow", String(Math.round(safeValue)));

  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;

  track.append(fill);
  return track;
}

export function createQuotaLabel(usage) {
  const label = document.createElement("div");
  label.className = "quota-label";

  const dot = document.createElement("span");
  dot.className = "quota-dot";

  const remaining = usage?.remaining ?? usage?.remainingToday ?? 0;
  const limit = usage?.dailyLimit ?? 0;

  const text = document.createElement("span");
  text.textContent = `Осталось ${remaining} из ${limit} фото сегодня`;

  label.append(dot, text);
  return label;
}

export function createSubscriptionHint(subscription) {
  const wrap = document.createElement("div");
  wrap.className = "subscription-hint";

  if (subscription?.status === "active") {
    wrap.textContent = `Премиум активен до ${formatDate(subscription.activeUntil)}`;
  } else {
    wrap.textContent = "Бесплатный тариф · 2 фото в день";
  }

  return wrap;
}

export function createPremiumGateCard(options = {}) {
  const {
    onCta = null,
    ctaText = "Получить Premium за 499 ₽",
    note = "",
  } = options;

  const card = document.createElement("section");
  card.className = "premium-gate-card";

  const badge = document.createElement("p");
  badge.className = "premium-gate-badge";
  badge.textContent = "✦ Premium";

  const title = document.createElement("h3");
  title.className = "premium-gate-title";
  title.textContent = "Расширенная аналитика доступна в Premium";

  const priceRow = document.createElement("div");
  priceRow.className = "premium-gate-price";

  const oldPrice = document.createElement("span");
  oldPrice.className = "premium-gate-price-old";
  oldPrice.textContent = "1 499 ₽";

  const currentPrice = document.createElement("span");
  currentPrice.className = "premium-gate-price-current";
  currentPrice.textContent = "499 ₽";

  const period = document.createElement("span");
  period.className = "premium-gate-price-period";
  period.textContent = "/ 30 дней";

  priceRow.append(oldPrice, currentPrice, period);

  const benefits = document.createElement("ul");
  benefits.className = "premium-gate-benefits";

  [
    "Расширенный недельный отчет",
    "Расширенный месячный отчет",
    "Разбор почему вес стоит",
    "График веса и динамики",
    "Гибкие напоминания",
  ].forEach((text) => {
    const item = document.createElement("li");
    item.className = "premium-gate-benefit";
    item.textContent = text;
    benefits.append(item);
  });

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn-primary-organic";
  button.style.marginTop = "1rem";
  button.textContent = ctaText;
  if (typeof onCta === "function") {
    button.addEventListener("click", onCta);
  }

  card.append(badge, title, priceRow, benefits, button);

  if (note) {
    const noteEl = document.createElement("p");
    noteEl.className = "premium-gate-note";
    noteEl.textContent = note;
    card.append(noteEl);
  }

  return card;
}

export function createFormField({ label, input }) {
  const field = document.createElement("label");
  field.className = "form-field";

  const text = document.createElement("span");
  text.className = "form-label";
  text.textContent = label;

  // Apply organic input styling
  input.className = input.className
    ? input.className.replace(/input\b|select\b|input-bordered\b/g, "").trim() + " input-organic"
    : "input-organic";

  field.append(text, input);
  return field;
}

export function createResultCard(result, options = {}) {
  const {
    mealTime = "",
    isPremium = false,
    hideWarningChip = false,
    onRetry = null,
  } = options;
  const section = document.createElement("section");
  section.className = "result-section";

  const totals = result?.totals || {};

  // ── Hero card ──
  const hero = document.createElement("section");
  hero.className = "result-hero";

  const badges = document.createElement("div");
  badges.className = "result-badges";

  if (mealTime) {
    const mealBadge = document.createElement("span");
    mealBadge.className = "result-badge result-badge-meal";
    mealBadge.textContent = mealTimeLabel(mealTime);
    badges.append(mealBadge);
  }

  if (isPremium) {
    const premiumBadge = document.createElement("span");
    premiumBadge.className = "result-badge result-badge-premium";
    premiumBadge.textContent = "✦ Premium";
    badges.append(premiumBadge);
  }

  const warnings = result?.warnings || [];
  if (warnings.length > 0 && !hideWarningChip) {
    const warningChip = document.createElement("span");
    warningChip.className = "result-badge result-badge-warning";
    warningChip.textContent = "Есть неточность";
    badges.append(warningChip);
  }

  if (badges.childElementCount > 0) {
    hero.append(badges);
  }

  const topLabel = document.createElement("p");
  topLabel.className = "result-hero-label";
  topLabel.textContent = "Общая оценка";

  const calories = document.createElement("p");
  calories.className = "result-hero-kcal";

  const kcalNum = document.createTextNode(`≈ ${formatCalories(totals.calories_kcal)}`);
  const kcalUnit = document.createElement("sup");
  kcalUnit.textContent = "ккал";
  calories.append(kcalNum, kcalUnit);

  const estimateCaption = document.createElement("p");
  estimateCaption.className = "result-hero-sub";
  estimateCaption.textContent = "Оценка по фото · возможна погрешность";

  // Confidence bar
  const confidence = Number(result?.overall_confidence ?? 0);
  const confRow = document.createElement("div");
  confRow.className = "result-confidence-row";

  const confLabel = document.createElement("span");
  confLabel.textContent = "Точность";

  const confBar = document.createElement("div");
  confBar.className = "result-confidence-bar";
  const confFill = document.createElement("div");
  confFill.className = "result-confidence-fill";
  confFill.style.width = `${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}%`;
  confBar.append(confFill);

  const confPct = document.createElement("span");
  confPct.textContent = `${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}%`;

  confRow.append(confLabel, confBar, confPct);

  // Macros grid
  const macroRow = document.createElement("div");
  macroRow.className = "result-macro-grid";
  macroRow.append(
    statBlock("Белки", `${formatMacro(totals.protein_g)} г`),
    statBlock("Жиры", `${formatMacro(totals.fat_g)} г`),
    statBlock("Углев", `${formatMacro(totals.carbs_g)} г`),
  );

  hero.append(topLabel, calories, estimateCaption, confRow, macroRow);
  section.append(hero);

  // ── Not recognized ──
  if (result?.recognized === false) {
    const empty = document.createElement("section");
    empty.className = "result-not-recognized";

    const icon = document.createElement("div");
    icon.className = "result-not-recognized-icon";
    icon.textContent = "📷";

    const title = document.createElement("p");
    title.className = "result-not-recognized-title";
    title.textContent = "Фото получилось не очень понятным";

    const text = document.createElement("p");
    text.className = "result-not-recognized-text";
    text.textContent = "Мы не смогли уверенно распознать блюдо. Попробуйте ещё раз.";

    const tips = document.createElement("div");
    tips.className = "result-not-recognized-tips";

    for (const tip of ["Больше света", "Снимайте сверху"]) {
      const chip = document.createElement("span");
      chip.className = "result-tip-chip";
      chip.textContent = tip;
      tips.append(chip);
    }

    empty.append(icon, title, text, tips);

    if (typeof onRetry === "function") {
      const retryBtn = document.createElement("button");
      retryBtn.type = "button";
      retryBtn.className = "btn-primary-organic";
      retryBtn.style.marginTop = "1rem";
      retryBtn.textContent = "Повторить";
      retryBtn.addEventListener("click", onRetry);
      empty.append(retryBtn);
    }

    section.append(empty);
  }

  // ── Items ──
  const items = result?.items || [];
  const itemsTitle = document.createElement("h3");
  itemsTitle.className = "result-section-title";
  itemsTitle.textContent = "Блюда";
  section.append(itemsTitle);

  if (items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "result-empty";
    empty.textContent = "Нет детализированных позиций.";
    section.append(empty);
  } else {
    const list = document.createElement("div");
    list.className = "result-items-list";
    for (const item of items) {
      const card = document.createElement("article");
      card.className = "result-item-card";

      const line1 = document.createElement("div");
      line1.className = "result-item-top";

      const left = document.createElement("div");
      left.className = "result-item-left";

      const name = document.createElement("strong");
      name.className = "result-item-name";
      name.textContent = toRussianItemName(item.name);
      left.append(name);

      const itemCalories = Number(item?.calories_kcal ?? 0);
      if (Number.isFinite(itemCalories) && itemCalories > 400) {
        const hotBadge = document.createElement("span");
        hotBadge.className = "result-item-badge-hot";
        hotBadge.textContent = "Калорийно";
        left.append(hotBadge);
      }

      if (item._kbju_source && item._kbju_source !== "exact") {
        const sourceBadge = document.createElement("span");
        sourceBadge.className = "result-item-badge-source";
        sourceBadge.textContent = item._kbju_source === "base" ? "базовое КБЖУ" : "примерное КБЖУ";
        left.append(sourceBadge);
      }

      const kcal = document.createElement("span");
      kcal.className = "result-item-kcal";
      kcal.textContent = `≈ ${formatCalories(item.calories_kcal)} ккал`;

      line1.append(left, kcal);

      const line2 = document.createElement("p");
      line2.className = "result-item-meta";
      line2.textContent = `Б ${formatMacro(item.protein_g)} · Ж ${formatMacro(item.fat_g)} · У ${formatMacro(item.carbs_g)} · ${formatMacro(item.grams)} г`;

      card.append(line1, line2);
      list.append(card);
    }
    section.append(list);
  }

  // ── Warnings ──
  if (warnings.length > 0) {
    const warningWrap = document.createElement("div");
    warningWrap.className = "result-warnings";
    for (const warning of warnings) {
      const line = document.createElement("p");
      line.className = "result-warning-text";
      line.textContent = warning;
      warningWrap.append(line);
    }
    section.append(warningWrap);
  }

  return section;
}

export function createHistoryList(items, onOpen) {
  const section = document.createElement("section");
  section.className = "history-list";

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "Пока нет приёмов пищи";
    section.append(empty);
    return section;
  }

  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    button.addEventListener("click", () => onOpen(item.id));

    const top = document.createElement("div");
    top.className = "history-item-top";

    const dt = document.createElement("span");
    dt.className = "history-item-time";
    dt.textContent = formatDateTime(item.createdAt);

    const mealLabels = {
      breakfast: "Завтрак",
      lunch: "Обед",
      dinner: "Ужин",
      snack: "Перекус",
      unknown: "—",
    };
    const mealBadge = document.createElement("span");
    mealBadge.className = "history-item-badge";
    mealBadge.textContent = mealLabels[item.mealTime] || "—";

    top.append(dt, mealBadge);

    const calories = document.createElement("div");
    calories.className = "history-item-kcal";

    const kcalNum = document.createTextNode(`${item.totals?.calories_kcal ?? 0}`);
    const kcalSup = document.createElement("sup");
    kcalSup.textContent = "ккал";
    calories.append(kcalNum, kcalSup);

    // Macro mini-line
    const macro = document.createElement("p");
    macro.className = "history-item-macro";
    const t = item.totals || {};
    macro.textContent = `Б ${formatMacro(t.protein_g)} · Ж ${formatMacro(t.fat_g)} · У ${formatMacro(t.carbs_g)}`;

    button.append(top, calories, macro);
    section.append(button);
  }

  return section;
}

export function showToast(message, variant = "error") {
  const toast = document.getElementById("toast");
  if (!toast) {
    return;
  }

  toast.className = "toast-base";
  if (variant === "warning") {
    toast.classList.add("alert-warning");
  } else if (variant === "info") {
    toast.classList.add("alert-info");
  } else {
    toast.classList.add("alert-error");
  }

  toast.classList.remove("toast-hidden");
  toast.classList.add("toast-visible");
  toast.hidden = false;
  toast.textContent = message;

  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.classList.remove("toast-visible");
    toast.classList.add("toast-hidden");
    window.setTimeout(() => {
      toast.hidden = true;
    }, 180);
  }, 2600);
}

showToast.timer = null;

export function createStreakBadge(streak, onClick) {
  const { currentStreak = 0 } = streak || {};

  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = "streak-badge";
  badge.setAttribute("aria-label", `Серия: ${currentStreak} дней`);

  const icon = document.createElement("span");
  icon.className = "streak-badge-icon";
  icon.textContent = "🔥";

  const count = document.createElement("span");
  count.className = "streak-badge-count";
  count.textContent = String(currentStreak);

  badge.append(icon, count);

  if (currentStreak >= 3) {
    badge.classList.add("streak-badge--active");
  } else if (currentStreak === 0) {
    badge.classList.add("streak-badge--muted");
  }

  if (typeof onClick === "function") {
    badge.addEventListener("click", onClick);
  }

  return badge;
}

export function createStreakModal(streak, onClose) {
  const { currentStreak = 0, bestStreak = 0 } = streak || {};

  const overlay = document.createElement("div");
  overlay.className = "streak-modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Информация о серии");

  const content = document.createElement("div");
  content.className = "streak-modal";

  const header = document.createElement("div");
  header.className = "streak-modal-header";

  const title = document.createElement("h3");
  title.className = "streak-modal-title";
  title.textContent = "🔥 Серия";

  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "streak-modal-close";
  closeBtn.setAttribute("aria-label", "Закрыть");
  closeBtn.innerHTML = "&times;";
  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (typeof onClose === "function") {
      onClose();
    }
  });

  header.append(title, closeBtn);

  const body = document.createElement("div");
  body.className = "streak-modal-body";

  const current = document.createElement("p");
  current.className = "streak-modal-current";
  current.innerHTML = `Ты держишься уже <strong>${currentStreak}</strong> ${pluralizeDays(currentStreak)} подряд`;

  const best = document.createElement("p");
  best.className = "streak-modal-best";
  best.innerHTML = `🏆 Лучший результат: <strong>${bestStreak}</strong> ${pluralizeDays(bestStreak)}`;

  body.append(current, best);
  content.append(header, body);
  overlay.append(content);

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay && typeof onClose === "function") {
      onClose();
    }
  });

  const handleKeydown = (e) => {
    if (e.key === "Escape" && typeof onClose === "function") {
      onClose();
    }
  };
  overlay.addEventListener("keydown", handleKeydown);

  return overlay;
}

function pluralizeDays(count) {
  const n = Math.abs(count);
  const lastTwo = n % 100;
  const lastOne = n % 10;

  if (lastTwo >= 11 && lastTwo <= 14) {
    return "дней";
  }
  if (lastOne === 1) {
    return "день";
  }
  if (lastOne >= 2 && lastOne <= 4) {
    return "дня";
  }
  return "дней";
}

export function createShareCard(options = {}) {
  const {
    currentStreak = 0,
    todayCalories = 0,
    dailyGoal = 2000,
    motivationalQuote = "",
  } = options;

  const container = document.createElement("div");
  container.className = "share-screen";

  if (currentStreak >= 10) {
    container.classList.add("share-screen--streak-strong");
  } else {
    container.classList.add("share-screen--streak");
  }

  const card = document.createElement("div");
  card.className = "share-card";

  const brand = document.createElement("div");
  brand.className = "share-brand";
  brand.textContent = "FitAI";
  card.append(brand);

  const flame = document.createElement("div");
  flame.className = "share-flame";
  flame.textContent = "🔥";

  if (currentStreak >= 10) {
    flame.classList.add("share-flame--glow-strong");
  } else if (currentStreak >= 5) {
    flame.classList.add("share-flame--glow");
  }

  const streakLabel = document.createElement("p");
  streakLabel.className = "share-streak-label";
  streakLabel.textContent = `${currentStreak} ${pluralizeDays(currentStreak)} подряд`;

  const streakSubtext = document.createElement("p");
  streakSubtext.className = "share-streak-subtext";
  streakSubtext.textContent = "держу режим питания";

  const hero = document.createElement("div");
  hero.className = "share-hero";
  hero.append(flame, streakLabel, streakSubtext);
  card.append(hero);

  const progressRing = document.createElement("div");
  progressRing.className = "share-progress-ring";

  const safeGoal = Math.max(1, Number(dailyGoal) || 1);
  const safeCalories = Math.max(0, Number(todayCalories) || 0);
  const percent = Math.min(100, Math.max(0, (safeCalories / safeGoal) * 100));
  const radius = 70;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (percent / 100) * circumference;
  const gradientId = `share-progress-gradient-${Math.random().toString(16).slice(2)}`;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 160 160");

  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const gradient = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
  gradient.setAttribute("id", gradientId);
  gradient.setAttribute("x1", "0%");
  gradient.setAttribute("y1", "0%");
  gradient.setAttribute("x2", "100%");
  gradient.setAttribute("y2", "100%");

  const stop1 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
  stop1.setAttribute("offset", "0%");
  stop1.setAttribute("stop-color", "#facc15");

  const stop2 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
  stop2.setAttribute("offset", "100%");
  stop2.setAttribute("stop-color", "#f97316");

  gradient.append(stop1, stop2);
  defs.append(gradient);
  svg.append(defs);

  const bgCircle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  bgCircle.setAttribute("class", "share-progress-ring-bg");
  bgCircle.setAttribute("cx", "80");
  bgCircle.setAttribute("cy", "80");
  bgCircle.setAttribute("r", String(radius));
  svg.append(bgCircle);

  const progressCircle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  progressCircle.setAttribute("class", "share-progress-ring-fill");
  progressCircle.setAttribute("cx", "80");
  progressCircle.setAttribute("cy", "80");
  progressCircle.setAttribute("r", String(radius));
  progressCircle.setAttribute("stroke", `url(#${gradientId})`);
  progressCircle.setAttribute("stroke-dasharray", String(circumference));
  progressCircle.setAttribute("stroke-dashoffset", String(circumference));
  svg.append(progressCircle);

  progressRing.append(svg);

  const ringContent = document.createElement("div");
  ringContent.className = "share-progress-content";

  const value = document.createElement("div");
  value.className = "share-progress-value";
  value.textContent = `${Math.round(safeCalories)}`;

  const target = document.createElement("div");
  target.className = "share-progress-target";
  target.textContent = `/ ${Math.round(safeGoal)} ккал`;

  ringContent.append(value, target);
  progressRing.append(ringContent);
  card.append(progressRing);

  const caption = document.createElement("p");
  caption.className = "share-caption";
  caption.textContent = "Сегодняшний прогресс";
  card.append(caption);

  const progressSummary = document.createElement("p");
  progressSummary.className = "share-progress-summary";
  progressSummary.textContent = `Сегодня: ${Math.round(safeCalories)} из ${Math.round(safeGoal)} ккал`;
  card.append(progressSummary);

  const benefits = document.createElement("div");
  benefits.className = "share-benefits";
  ["Фото → калории за секунды", "Счётчик стрика", "Дневной прогресс"].forEach((text) => {
    const chip = document.createElement("span");
    chip.className = "share-benefit-chip";
    chip.textContent = text;
    benefits.append(chip);
  });
  card.append(benefits);

  const hint = document.createElement("p");
  hint.className = "share-hint";
  hint.textContent = motivationalQuote || pickRandomQuote(QUOTES_SHARE_SHORT).text;
  card.append(hint);

  const footer = document.createElement("div");
  footer.className = "share-footer";
  footer.textContent = "@fitai_calc_bot";
  card.append(footer);

  container.append(card);

  window.requestAnimationFrame(() => {
    progressCircle.style.strokeDashoffset = String(offset);
  });

  return container;
}
