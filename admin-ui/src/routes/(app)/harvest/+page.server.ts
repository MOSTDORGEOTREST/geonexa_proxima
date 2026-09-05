import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	const [profile, stats, reasons, cursors, runs, schedules, health, active] = await Promise.all([
		tryApi<any>('/api/admin/harvest/profile', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/harvest/terms/stats', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/harvest/blocked-reasons', { token: locals.token, fetchImpl: fetch }),
		tryApi<any[]>('/api/admin/harvest/cursors', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/harvest/runs', {
			token: locals.token,
			query: { per_page: 10 },
			fetchImpl: fetch
		}),
		// Расписание сбора нужно здесь ради одной кнопки «Собрать статьи»:
		// человек, которому нужен прогон, приходит на эту страницу, а не в
		// «Запуски», и не должен искать запуск в другом разделе.
		tryApi<any[]>('/api/admin/schedules', {
			token: locals.token,
			query: { kind: 'global_harvest' },
			fetchImpl: fetch
		}),
		tryApi<any>('/api/admin/prefect/health', { token: locals.token, fetchImpl: fetch }),
		// Зависший прогон блокирует весь сбор — про него надо сказать сразу,
		// а не оставлять человека гадать, почему кнопка не работает.
		tryApi<any>('/api/admin/harvest/runs', {
			token: locals.token,
			query: { status: 'running', per_page: 1 },
			fetchImpl: fetch
		})
	]);
	return {
		profile,
		stats,
		reasons: reasons ?? [],
		cursors: cursors ?? [],
		runs: runs?.items ?? [],
		harvestSchedule: (schedules ?? [])[0] ?? null,
		health,
		activeRun: (active?.items ?? [])[0] ?? null
	};
};

export const actions: Actions = {
	/** Ручной прогон сбора. Параметры приходят из формы: обычное окно или глубокое. */
	collect: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const label = String(form.get('label') ?? 'Сбор материалов');
		let parameters: Record<string, unknown> = {};
		try {
			parameters = JSON.parse(String(form.get('parameters') ?? '{}'));
		} catch {
			parameters = {};
		}
		if (!id) return fail(409, { error: 'Расписание сбора отсутствует в базе — запускать нечего' });
		// Параметры кнопки дополняют параметры расписания, а не заменяют их:
		// API подставляет `payload.parameters or row.parameters`, то есть
		// переданный объект вытесняет сохранённые целиком.
		if (Object.keys(parameters).length) {
			const stored = await tryApi<any[]>('/api/admin/schedules', {
				token: locals.token,
				query: { kind: 'global_harvest' },
				fetchImpl: fetch
			});
			parameters = { ...((stored ?? [])[0]?.parameters ?? {}), ...parameters };
		}
		try {
			const result = await api<any>(`/api/admin/schedules/${id}/run`, {
				method: 'POST',
				token: locals.token,
				body: Object.keys(parameters).length ? { parameters } : {},
				fetchImpl: fetch
			});
			return { started: result.flow_run_id ?? true, label };
		} catch (error) {
			return fail(503, { error: (error as Error).message });
		}
	},
	/** Снять прогон, который никто не закрыл. */
	abort: async ({ locals, fetch }) => {
		try {
			const result = await api<any>('/api/admin/harvest/runs/abort', {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { aborted: result?.aborted ?? 0 };
		} catch (error) {
			return fail(503, { error: (error as Error).message });
		}
	},
	/** Перечитать config/harvest.yaml в базу: экран профиля и гейт снова про одно. */
	resync: async ({ locals, fetch }) => {
		try {
			const result = await api<any>('/api/admin/harvest/profile/resync', {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { resynced: result };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	},
	probe: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const title = String(form.get('title') ?? '').trim();
		if (!title) return fail(400, { error: 'Введите заголовок' });
		try {
			const result = await api<any>('/api/admin/harvest/test', {
				method: 'POST',
				token: locals.token,
				body: {
					title,
					abstract: String(form.get('abstract') ?? '') || null,
					venue: String(form.get('venue') ?? '') || null
				},
				fetchImpl: fetch
			});
			return { probe: result, title };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	}
};
