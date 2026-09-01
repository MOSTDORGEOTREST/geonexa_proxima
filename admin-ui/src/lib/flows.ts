/** Человеческие имена оркестратора.
 *
 * В базе флоу называются ключами вроде `digest-dispatch-chats`, а этапы —
 * значениями `schedules.kind`. Администратору эти слова ничего не говорят:
 * ему нужно «собрать статьи» и «разослать в группы». Здесь одно место, где
 * машинный словарь переводится в человеческий, — и таблица расписаний, и
 * кнопки ручного запуска берут названия отсюда, поэтому разъехаться не могут.
 */

export type FlowAction = {
	/** Уникален в пределах страницы: один и тот же флоу бывает под двумя кнопками. */
	id: string;
	/** `schedules.key` — по нему находим строку расписания и её id. */
	key: string;
	label: string;
	hint: string;
	/** Дописывается поверх параметров расписания, а не вместо них. */
	parameters?: Record<string, unknown>;
	primary?: boolean;
};

export type FlowStage = {
	id: string;
	title: string;
	summary: string;
	/** Значения `schedules.kind`, попадающие в этот этап. */
	kinds: string[];
	actions: FlowAction[];
};

/** Этапы идут в порядке конвейера: собрали → отобрали → отправили → прибрали. */
export const STAGES: FlowStage[] = [
	{
		id: 'harvest',
		title: 'Сбор материалов',
		summary:
			'Обход источников, отсев по терминам и глобальная научная оценка. Общая для всех подписчиков: персонализация начинается позже.',
		kinds: ['global_harvest'],
		actions: [
			{
				id: 'harvest-now',
				key: 'global-harvest',
				label: 'Собрать статьи',
				hint: 'Обычное окно сбора — ровно то же, что делает расписание',
				primary: true
			},
			{
				id: 'harvest-deep',
				key: 'global-harvest',
				label: 'Собрать за 30 дней',
				hint: 'Догнать источники после простоя: тридцать суток по одним, тридцать проходов. Дольше и дороже по токенам',
				// Именно `days_back`, а не `lookback_hours`: второе включает
				// запасной режим одного открытого окна, который упирается в
				// лимит выдачи источника и молча теряет хвост.
				parameters: { days_back: 30, limit_per_source: 200 }
			}
		]
	},
	{
		id: 'digest',
		title: 'Подготовка дайджестов',
		summary:
			'Кому пора получить выпуск, что в него войдёт и в каком порядке. Диспетчер находит профили и запускает персональные флоу.',
		kinds: ['digest_dispatch', 'subscriber_digest'],
		actions: [
			{
				id: 'digest-users',
				key: 'digest-dispatch',
				label: 'Собрать для личных чатов',
				hint: 'Только подписчики-люди',
				primary: true
			},
			{
				id: 'digest-chats',
				key: 'digest-dispatch-chats',
				label: 'Собрать для групп и каналов',
				hint: 'У чатов своя частота и свои лимиты Bot API',
				primary: true
			},
			{
				id: 'digest-dry',
				key: 'digest-dispatch',
				label: 'Репетиция без отправки',
				hint: 'Дайджесты соберутся и останутся в статусе ready — в Telegram не уйдёт ничего',
				parameters: { deliver: false }
			}
		]
	},
	{
		id: 'delivery',
		title: 'Рассылка',
		summary:
			'Очередь отправки: задания разбираются по одному, с ретраями и учётом лимитов Telegram. Личные чаты и группы разведены — лимиты разные.',
		kinds: ['delivery_personal', 'delivery_group'],
		actions: [
			{
				id: 'deliver-personal',
				key: 'delivery-personal',
				label: 'Разослать в личные чаты',
				hint: 'Разобрать очередь личных сообщений сейчас',
				primary: true
			},
			{
				id: 'deliver-group',
				key: 'delivery-group',
				label: 'Разослать в группы и каналы',
				hint: 'Разобрать очередь чатов сейчас',
				primary: true
			}
		]
	},
	{
		id: 'maintenance',
		title: 'Обслуживание',
		summary:
			'То, что держит систему в порядке: права бота в чатах, сроки подписок, суточные агрегаты и уборка очереди.',
		kinds: ['chat_monitor', 'maintenance'],
		actions: [
			{
				id: 'chats',
				key: 'chat-monitor',
				label: 'Проверить права бота',
				hint: 'Сверка по всем группам и каналам: где бота выгнали и где он не может публиковать'
			},
			{
				id: 'subs',
				key: 'subscription-maintenance',
				label: 'Продления и просрочки',
				hint: 'Погасить истёкшие подписки и напомнить тем, у кого срок близко'
			},
			{
				id: 'metrics',
				key: 'metrics-rollup',
				label: 'Пересчитать метрики',
				hint: 'Суточные агрегаты за последние дни'
			},
			{
				id: 'queue',
				key: 'maintenance',
				label: 'Прибрать очередь',
				hint: 'Протухшие и зависшие задания рассылки'
			}
		]
	}
];

/** Состояния прогона в Prefect — по-русски. Ключи в нижнем регистре. */
export const RUN_STATE_LABELS: Record<string, string> = {
	scheduled: 'запланирован',
	pending: 'ожидает',
	running: 'выполняется',
	completed: 'успешно',
	failed: 'ошибка',
	crashed: 'аварийно завершён',
	cancelling: 'отменяется',
	cancelled: 'отменён',
	paused: 'приостановлен'
};

/** Состояния, после которых прогон уже не изменится. */
export const TERMINAL_STATES = new Set(['completed', 'failed', 'crashed', 'cancelled']);

/** Прогон занят машиной прямо сейчас.
 *
 *  `scheduled` сюда не входит намеренно: запланированный прогон может ждать
 *  часами, и называть это «выполняется» — врать. Зато он влияет на то, нужно
 *  ли обновлять страницу: со временем он станет running сам.
 */
export function isLive(state: string | null | undefined): boolean {
	return ['running', 'pending', 'cancelling', 'paused'].includes(String(state ?? '').toLowerCase());
}

export function isScheduled(state: string | null | undefined): boolean {
	return String(state ?? '').toLowerCase() === 'scheduled';
}

/** Сначала то, что уже началось, потом очередь.
 *
 *  Prefect отдаёт запланированные прогоны вперемешку с настоящими, а времени
 *  старта у них нет — при сортировке по нему они всплывают наверх и закрывают
 *  собой ровно то, ради чего на страницу пришли: последний реальный прогон и
 *  чем он кончился.
 */
export function byRecency<T extends { started_at?: string | null }>(runs: T[]): T[] {
	const started = (run: T) => (run.started_at ? 1 : 0);
	return [...runs].sort((a, b) =>
		started(a) !== started(b)
			? started(b) - started(a)
			: String(b.started_at ?? '').localeCompare(String(a.started_at ?? ''))
	);
}
