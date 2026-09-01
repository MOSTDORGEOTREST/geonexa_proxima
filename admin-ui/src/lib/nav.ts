/** Карта разделов админки.
 *
 * Двенадцать равноправных пунктов в шапке — это не навигация, а список всего,
 * что умеет система: глазу не за что зацепиться, и «Чаты» стоят рядом с
 * «Моделями», хотя ходят в них по разным поводам. Здесь пункты собраны в пять
 * разделов по одному вопросу: «в каком качестве я сюда пришёл» — смотрю сводку,
 * веду аудиторию, настраиваю сбор, слежу за работой платформы, чиню саму
 * платформу.
 *
 * Адреса страниц не меняются: раздел — это способ их показать, а не переезд.
 * Закладки, ссылки из писем и переходы внутри страниц продолжают работать.
 */

export type NavItem = {
	href: string;
	label: string;
	/** Показывать ли счётчик заявок. Он один на всю админку. */
	pending?: boolean;
};

export type NavSection = {
	id: string;
	label: string;
	items: NavItem[];
};

export const SECTIONS: NavSection[] = [
	{
		id: 'overview',
		label: 'Дашборд',
		items: [{ href: '/', label: 'Дашборд' }]
	},
	{
		id: 'audience',
		label: 'Аудитория',
		// Порядок — путь подписчика: заявка, подтверждённый чат, права бота,
		// оплата. Раздел открывается на заявках: это единственная страница со
		// счётчиком, то есть единственная, где может ждать работа.
		items: [
			{ href: '/moderation', label: 'Заявки', pending: true },
			{ href: '/subscribers', label: 'Подписчики' },
			{ href: '/chats', label: 'Чаты' },
			{ href: '/subscriptions', label: 'Подписки' }
		]
	},
	{
		id: 'harvest',
		label: 'Сбор',
		items: [{ href: '/harvest', label: 'Сбор' }]
	},
	{
		id: 'ops',
		label: 'Работа платформы',
		items: [
			{ href: '/runs', label: 'Запуски' },
			{ href: '/deliveries', label: 'Доставки' }
		]
	},
	{
		id: 'system',
		label: 'Система',
		items: [
			{ href: '/models', label: 'Модели' },
			{ href: '/settings', label: 'Настройки' },
			{ href: '/audit', label: 'Аудит' }
		]
	}
];

/** Совпадает ли адрес с пунктом. Корень — только точным совпадением. */
export function isActive(href: string, pathname: string): boolean {
	return href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);
}

/** Раздел, которому принадлежит адрес. `null` для страниц вне разделов.
 *
 * Вне разделов живёт, например, инструкция по профилю: на неё приходят по
 * ссылке из редактора профиля, и отдельного пункта в шапке она не заслуживает.
 */
export function sectionOf(pathname: string): NavSection | null {
	for (const section of SECTIONS) {
		if (section.items.some((item) => isActive(item.href, pathname))) return section;
	}
	return null;
}

/** Куда ведёт сам заголовок раздела — на его первую страницу. */
export function entryOf(section: NavSection): string {
	return section.items[0]?.href ?? '/';
}

/** Нужна ли строка вкладок: ради единственной страницы её показывать незачем. */
export function hasTabs(section: NavSection | null): boolean {
	return Boolean(section && section.items.length > 1);
}
