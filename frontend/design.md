# FitAI Frontend Design System (MVP Freeze)
Цель: современный минималистичный UI уровня “продукт”, без лишней сложности.  
Стек: **Vite + Vanilla JS + Tailwind CSS + DaisyUI + Inter + Lucide icons**.

---

## 1) Дизайн-принципы
1. **Иерархия важнее украшений**: крупные числа, ясные заголовки, спокойный фон.
2. **Whitespace = премиальность**: щедрые отступы, минимум рамок.
3. **Одна главная кнопка на экран** (Primary CTA).
4. **Состояния важнее компонентов**: loading/disabled/error должны выглядеть аккуратно.
5. **Мобильный-first**: max ширина контента `max-w-md`, всё кликабельное, крупные тапы.

---

## 2) Тема и токены
### Цвета (Tailwind tokens)
- Background: `bg-gray-50`
- Surface (cards): `bg-white`
- Text primary: `text-gray-900`
- Text secondary: `text-gray-600`
- Muted: `text-gray-400`
- Border subtle: `border-gray-100` / `border-gray-200`
- Primary (brand): `emerald` (например `btn-primary`, `text-emerald-600`, `bg-emerald-50`)
- Success: `emerald`
- Warning: `amber`
- Error: `red`

**Правило:** акцентный цвет — только для CTA, прогресса и статуса Premium. Остальное — серое/нейтральное.

### Типографика
- Font: **Inter**
- Заголовок экрана: `text-xl font-semibold`
- Крупная метрика (калории): `text-3xl font-semibold tracking-tight`
- Обычный текст: `text-sm text-gray-700`
- Подписи: `text-xs text-gray-500`
- Микротекст/дисклеймер: `text-xs text-gray-400`

### Радиусы и тени
- Карточки: `rounded-2xl shadow-sm`
- Внутренние блоки: `rounded-xl`
- Кнопки: `rounded-xl`
- Тени: только `shadow-sm` / `shadow-md` (редко)

### Сетка и отступы (8px grid)
- Экран: `px-4 py-6`
- Между секциями: `mb-6`
- Card padding: `p-5`
- Внутренние элементы: `gap-3` / `gap-4`

---

## 3) Layout scaffolding
Базовая оболочка всех экранов:

```html
<div class="min-h-screen bg-gray-50 text-gray-900">
  <div class="max-w-md mx-auto px-4 py-6">
    <!-- header -->
    <!-- content -->
  </div>
</div>

