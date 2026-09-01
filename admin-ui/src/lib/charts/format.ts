/** Форматирование чисел и дат в интерфейсе — одно на всё приложение. */

const NUMBER = new Intl.NumberFormat('ru-RU');
const DATE = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit' });
const DATETIME = new Intl.DateTimeFormat('ru-RU', {
	day: '2-digit',
	month: '2-digit',
	year: 'numeric',
	hour: '2-digit',
	minute: '2-digit'
});

export const n = (value: unknown): string =>
	value === null || value === undefined || value === '' ? '—' : NUMBER.format(Number(value));

export const day = (value: unknown): string =>
	value ? DATE.format(new Date(String(value))) : '—';

export const when = (value: unknown): string =>
	value ? DATETIME.format(new Date(String(value))) : '—';

export function ago(value: unknown): string {
	if (!value) return '—';
	const seconds = (Date.now() - new Date(String(value)).getTime()) / 1000;
	if (seconds < 90) return 'только что';
	const minutes = Math.round(seconds / 60);
	if (minutes < 90) return `${minutes} мин назад`;
	const hours = Math.round(minutes / 60);
	if (hours < 36) return `${hours} ч назад`;
	return `${Math.round(hours / 24)} дн назад`;
}

/** Секунды в «2 ч 15 мин» — очередь измеряется в ожидании, а не в числах. */
export function duration(seconds: unknown): string {
	const total = Number(seconds);
	if (!Number.isFinite(total) || total <= 0) return '—';
	if (total < 60) return `${Math.round(total)} с`;
	if (total < 3600) return `${Math.round(total / 60)} мин`;
	const hours = Math.floor(total / 3600);
	const minutes = Math.round((total % 3600) / 60);
	return minutes ? `${hours} ч ${minutes} мин` : `${hours} ч`;
}
