import { api, tryApi } from '$lib/api/client';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, fetch }) => {
	const queue = await tryApi<any>('/api/admin/subscribers/pending', {
		token: locals.token,
		fetchImpl: fetch
	});
	return { queue };
};

export const actions: Actions = {
	approve: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const description = String(form.get('description') ?? '').trim();
		try {
			const result = await api<any>(`/api/admin/subscribers/${id}/approve`, {
				method: 'POST',
				token: locals.token,
				// Описание интересов заполняется тем же действием: подтвердить чат и
				// тут же забыть про профиль — обычная ошибка, а чат без профиля
				// молча не попадает ни в один дайджест.
				body: { notify: true, grant_trial: true, description: description || null },
				fetchImpl: fetch
			});
			return { ok: true, approval: result?.approval ?? null };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	},
	reject: async ({ request, locals, fetch }) => {
		const form = await request.formData();
		const id = String(form.get('id') ?? '');
		const reason = String(form.get('reason') ?? '').trim();
		try {
			await api(`/api/admin/subscribers/${id}/reject`, {
				method: 'POST',
				token: locals.token,
				body: { reason: reason || null },
				fetchImpl: fetch
			});
			return { ok: true, rejected: true };
		} catch (error) {
			return fail(400, { error: (error as Error).message });
		}
	}
};
