import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	const [schedules, health, runs] = await Promise.all([
		tryApi<any[]>('/api/admin/schedules', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/prefect/health', { token: locals.token, fetchImpl: fetch }),
		// Без запланированных: здесь показываем, что уже отработало. Очередь
		// целиком видна на «Прогонах», а тут двадцать «запланирован» вытеснили
		// бы из выборки последний настоящий прогон.
		tryApi<any[]>('/api/admin/prefect/flow-runs', {
			token: locals.token,
			query: { limit: 20, include_scheduled: false },
			fetchImpl: fetch
		})
	]);
	return { schedules: schedules ?? [], health, runs: runs ?? [] };
};

export const actions: Actions = {
	run: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const label = String(form.get('label') ?? 'Флоу');
		// Параметры приходят уже слитыми с параметрами расписания: пустой объект
		// означает «взять то, что записано в расписании», и это разные вещи.
		let parameters: Record<string, unknown> = {};
		try {
			parameters = JSON.parse(String(form.get('parameters') ?? '{}'));
		} catch {
			parameters = {};
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
	toggle: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			await api(`/api/admin/schedules/${id}/toggle`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { ok: true };
		} catch (error) {
			return fail(503, { error: (error as Error).message });
		}
	},
	cron: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const cron = String(form.get('cron') ?? '').trim();
		try {
			await api(`/api/admin/schedules/${id}`, {
				method: 'PATCH',
				token: locals.token,
				body: { cron },
				fetchImpl: fetch
			});
			return { ok: true };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	},
	resync: async ({ locals, fetch }) => {
		try {
			const result = await api<any>('/api/admin/prefect/resync', {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return {
				resynced: (result?.synced ?? []).length,
				resyncFailed: Object.keys(result?.failed ?? {}).length
			};
		} catch (error) {
			return fail(503, { error: (error as Error).message });
		}
	}
};
