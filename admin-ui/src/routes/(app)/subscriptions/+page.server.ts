import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url, fetch }) => {
	const [plans, list, expiring] = await Promise.all([
		tryApi<any[]>('/api/admin/plans', { token: locals.token, fetchImpl: fetch }),
		tryApi<any>('/api/admin/subscriptions', {
			token: locals.token,
			query: { page: url.searchParams.get('page') ?? 1, status: url.searchParams.get('status') ?? undefined },
			fetchImpl: fetch
		}),
		tryApi<any[]>('/api/admin/subscriptions/expiring', {
			token: locals.token,
			query: { days: 7 },
			fetchImpl: fetch
		})
	]);
	return { plans: plans ?? [], list, expiring: expiring ?? [] };
};

export const actions: Actions = {
	extend: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		try {
			await api(`/api/admin/subscriptions/${form.get('id')}/extend`, {
				method: 'POST',
				token: locals.token,
				body: { days: Number(form.get('days') ?? 30) },
				fetchImpl: fetch
			});
			return { ok: true };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	},
	cancel: async ({ request, locals, fetch }) => {
		const id = String((await request.formData()).get('id') ?? '');
		try {
			await api(`/api/admin/subscriptions/${id}/cancel`, {
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
