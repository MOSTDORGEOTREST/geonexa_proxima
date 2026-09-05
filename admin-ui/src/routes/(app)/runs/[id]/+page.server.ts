import { api, tryApi } from '$lib/api/client';
import { error, fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch, params }) => {
	const [runs, logs] = await Promise.all([
		tryApi<any[]>('/api/admin/prefect/flow-runs', {
			token: locals.token,
			query: { limit: 200 },
			fetchImpl: fetch
		}),
		tryApi<any[]>(`/api/admin/prefect/flow-runs/${params.id}/logs`, {
			token: locals.token,
			query: { limit: 1000 },
			fetchImpl: fetch
		})
	]);
	// Отдельного эндпоинта на один прогон нет: берём его из общего списка.
	// Список ограничен, поэтому очень старый прогон может не найтись — это
	// честнее показать как 404, чем как пустую страницу без объяснения.
	const run = (runs ?? []).find((item) => String(item.id) === params.id);
	if (!run && !(logs ?? []).length) {
		throw error(404, 'Прогон не найден: возможно, он уже вытеснен из истории Prefect');
	}
	return { run: run ?? { id: params.id }, logs: logs ?? [] };
};

export const actions: Actions = {
	cancel: async ({ locals, fetch, params }) => {
		try {
			await api(`/api/admin/prefect/flow-runs/${params.id}/cancel`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { cancelled: true };
		} catch (err) {
			return fail(503, { error: (err as Error).message });
		}
	}
};
