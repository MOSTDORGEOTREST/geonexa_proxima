import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const [queue, jobs] = await Promise.all([
		tryApi<any>('/api/admin/deliveries/queue', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/deliveries/jobs', {
			token: locals.token,
			query: {
				page: url.searchParams.get('page') ?? 1,
				status: url.searchParams.get('status') ?? undefined,
				channel: url.searchParams.get('channel') ?? undefined
			},
			fetchImpl: fetch
		})
	]);
	return { queue, jobs, filters: Object.fromEntries(url.searchParams) };
};

export const actions: Actions = {
	retry: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			await api(`/api/admin/deliveries/jobs/${id}/retry`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { ok: true };
		} catch (error) {
			return fail(409, { error: (error as Error).message });
		}
	},
	cancel: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			await api(`/api/admin/deliveries/jobs/${id}/cancel`, {
				method: 'POST',
				token: locals.token,
				fetchImpl: fetch
			});
			return { ok: true };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	}
};
