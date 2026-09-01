/**
 * Палитра данных. Значения провалидированы, см. docs/design.md §5.
 *
 * Слот закреплён за сущностью и приходит с бэкенда, а не выбирается по индексу
 * массива: иначе фильтр, убравший одну серию, перекрасил бы остальные, и
 * читатель графика решил бы, что изменились данные.
 */

export const SLOTS = ['var(--s1)', 'var(--s2)', 'var(--s3)'] as const;

export const STATUS = {
	good: 'var(--good)',
	warning: 'var(--warning)',
	critical: 'var(--critical)'
} as const;

export type ColorSlot = number | keyof typeof STATUS;

/** Цвет по слоту: число — категориальная серия, строка — статус. */
export function slotColor(slot: ColorSlot | undefined, fallbackIndex = 0): string {
	if (typeof slot === 'string' && slot in STATUS) return STATUS[slot as keyof typeof STATUS];
	if (typeof slot === 'number' && slot >= 1 && slot <= SLOTS.length) return SLOTS[slot - 1];
	// Девятый тон не генерируется: за пределами слотов серия обязана
	// сворачиваться в «Прочее», а не получать выдуманный цвет.
	return SLOTS[fallbackIndex % SLOTS.length];
}

/** Ординальная шкала воронки: один тон, монотонная светлота. */
export const FUNNEL = ['#8F5A10', '#A66A14', '#C38316', '#E8A33D', '#FFC96B'];

export function funnelColor(step: number, total: number): string {
	if (total <= 1) return FUNNEL[FUNNEL.length - 1];
	const index = Math.round((step / (total - 1)) * (FUNNEL.length - 1));
	return FUNNEL[Math.min(FUNNEL.length - 1, Math.max(0, index))];
}
