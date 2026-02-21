# FitAI Design System — Fitness Style (MVP Freeze)
Стиль: **Fitness / Health** — энергичный, “чистый спортзал”, но без кричащих цветов.  
Цель: выглядит как современный продукт, мотивирует, хорошо читается в Telegram WebApp, легко поддерживать (one-shot MVP).

Стек: **Vite + Vanilla JS + Tailwind CSS + DaisyUI + Inter + Lucide icons**.

---

## 1) Основные принципы Fitness UI
1. **Энергия через акценты, не через шум**: яркость только в 1–2 местах (progress/CTA/премиум).
2. **Чёткие метрики**: калории/прогресс — главный герой.
3. **Карточки = блоки тренировки**: всё группируется в card-sections.
4. **Состояния важны**: loading/error/disabled должны быть “премиально спокойными”.
5. **Доступность**: крупные тапы, высокий контраст, читаемо на солнце.

---

## 2) Цветовая система (Tokens)
Fitness-стиль: свежий, “энергия + здоровье”.

### Базовые
- Background: `bg-slate-50`
- Surface: `bg-white`
- Text primary: `text-slate-900`
- Text secondary: `text-slate-600`
- Muted: `text-slate-400`
- Border subtle: `border-slate-100` / `border-slate-200`

### Акценты
- **Primary (Energy/CTA):** `lime` / `emerald` гибрид
  - Рекомендуемый: `lime-500` для progress/CTA
  - Альтернатива (более спокойная): `emerald-500`

- **Progress fill:** `lime` (ощущение “заряда”)
- **Premium:** `violet-600` (контрастно и “премиально”)
- **Warning:** `amber-500`
- **Error:** `rose-500`
- **Info:** `sky-500`

**Правило:**  
- Lime/emerald — только про действие и прогресс.  
- Violet — только про Premium.  
- Остальное — нейтральное.

---

## 3) Типографика
Шрифт: **Inter**.

- Screen title: `text-xl font-semibold tracking-tight`
- Primary metric (ккал): `text-3xl font-semibold tracking-tight`
- Secondary metric: `text-sm font-medium text-slate-700`
- Body: `text-sm text-slate-700`
- Caption: `text-xs text-slate-500`
- Micro/disclaimer: `text-xs text-slate-400`

Числа (калории/прогресс) должны быть **крупнее текста** минимум в 2 раза.

---

## 4) Радиусы, тени, spacing
### Радиусы
- Main cards: `rounded-2xl`
- Inner blocks: `rounded-xl`
- Buttons: `rounded-xl`
- Chips/badges: `rounded-full`

### Тени
- Cards: `shadow-sm`
- Paywall hero card: `shadow-md`
- Без “жирных” теней.

### Отступы (8px grid)
- Screen padding: `px-4 py-6`
- Section gap: `mb-6`
- Card padding: `p-5`
- Inner gap: `gap-3` / `gap-4`

---

## 5) Layout scaffolding
Оболочка каждого экрана:

```html
<div class="min-h-screen bg-slate-50 text-slate-900">
  <div class="max-w-md mx-auto px-4 py-6">
    <!-- header -->
    <!-- content -->
  </div>
</div>
