import { api, ApiError } from '$lib/api/client';
import { error } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch, params }) => {
	try {
		return await api<any>(`/api/admin/items/${params.id}`, {
			token: locals.token,
			fetchImpl: fetch
		});
	} catch (err) {
		if (err instanceof ApiError && err.status === 404) {
			throw error(404, 'Публикация не найдена');
		}
		throw error(503, `API не ответил: ${(err as Error).message}`);
	}
};
